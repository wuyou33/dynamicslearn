import os
import sys
from dotmap import DotMap

import opto
import opto.data as rdata
from opto.opto.classes.OptTask import OptTask
from opto.opto.classes import StopCriteria, Logs
from opto.utils import bounds
from opto.opto.acq_func import EI, UCB
from opto import regression

import pandas as pd
import numpy as np
import torch
import math

from learn.control.pid import PID
from learn.control.pid import PidPolicy
from learn.utils.data import cwd_basedir
import logging
import hydra

log = logging.getLogger(__name__)

# Bayesian Optimization Class build on opto

'''Defines a class and numerous methods for performing Bayesian Optimization
    Variables: - PID_Object: object to Opto object to optimize
               - PIDMODE: PID policy mode (euler...rate etc)
               - n_parameters: number of total PID parameters for the policy we are testing
               - task: an opttask that is to be optimized through the Opto library
               - Stop: stop criteria for BO
               - sim: boolean signalling whether we are using a simulation (includes position in state)
    Methods: - optimize: uses opto library and EI acquisition to perform BO
             - getParameters: returns the results of bayesian optimization
'''


class BOPID():
    def __init__(self, bo_cfg, policy_cfg, opt_function):
        # self.Objective = SimulationOptimizer(bo_cfg, policy_cfg)
        self.b_cfg = bo_cfg
        self.p_cfg = policy_cfg

        self.PIDMODE = policy_cfg.mode
        self.policy = PidPolicy(policy_cfg)
        evals = bo_cfg.iterations
        param_min = list(policy_cfg.pid.params.min_values)
        param_max = list(policy_cfg.pid.params.max_values)
        self.n_parameters = self.policy.numParameters
        self.n_pids = self.policy.numpids
        params_per_pid = self.n_parameters / self.n_pids
        assert params_per_pid % 1 == 0
        params_per_pid = int(params_per_pid)

        self.task = OptTask(f=opt_function, n_parameters=self.n_parameters, n_objectives=1,
                            bounds=bounds(min=param_min * params_per_pid,
                                          max=param_max * params_per_pid), task={'minimize'},
                            vectorized=False)
        # labels_param = ['KP_pitch','KI_pitch','KD_pitch', 'KP_roll' 'KI_roll', 'KD_roll', 'KP_yaw', 'KI_yaw', 'KD_yaw', 'KP_pitchRate', 'KI_pitchRate', 'KD_pitchRate', 'KP_rollRate',
        # 'KI_rollRate', 'KD_rollRate', "KP_yawRate", "KI_yawRate", "KD_yawRate"])
        self.Stop = StopCriteria(maxEvals=evals)
        self.sim = bo_cfg.sim

    def optimize(self):
        p = DotMap()
        p.verbosity = 1
        p.acq_func = EI(model=None, logs=None)
        # p.acq_func = UCB(model=None, logs=None)
        p.model = regression.GP
        self.opt = opto.BO(parameters=p, task=self.task, stopCriteria=self.Stop)
        self.opt.optimize()

    def getParameters(self):
        log = self.opt.get_logs()
        losses = log.get_objectives()
        best = log.get_best_parameters()
        bestLoss = log.get_best_objectives()
        nEvals = log.get_n_evals()
        best = [matrix.tolist() for matrix in best]  # can be a buggy line

        print("Best PID parameters found with loss of: ", np.amin(bestLoss), " in ", nEvals, " evaluations.")
        print("Pitch:   Prop: ", best[0], " Int: ", 0, " Deriv: ", best[1])
        print("Roll:    Prop: ", best[2], " Int: ", 0, " Deriv: ", best[3])

        return log

    def basic_rollout(self, s0, i_model):
        # todo need to accound for history automatically
        max_len = self.b_cfg.max_length
        cur_action = self.policy.get_action(s0)
        next_state, logvars = smart_model_step(i_model, s0, cur_action)
        state = push_history(next_state, s0)
        cost = 0
        for k in range(max_len):
            # print(f"Itr {k}")
            # print(f"Action {cur_action.tolist()}")
            # print(f"State {next_state.tolist()}")
            cur_action = self.policy.get_action(next_state)
            next_state, logvars = smart_model_step(i_model, state, cur_action)
            state = push_history(next_state, state)
            # print(f"logvars {logvars}")
            # weight = 0 if k < 5 else 1
            weight = .9 ** (max_len - k)
            cost += weight * get_reward_iono(next_state, cur_action)

        return cost / max_len  # cost


def get_reward_iono(next_ob, action):
    # Going to make the reward -c(x) where x is the attitude based cost
    assert isinstance(next_ob, np.ndarray)
    assert isinstance(action, np.ndarray)
    assert next_ob.ndim in (1, 2)

    was1d = next_ob.ndim == 1
    if was1d:
        next_ob = np.expand_dims(next_ob, 0)
        action = np.expand_dims(action, 0)
    assert next_ob.ndim == 2

    pitch = np.divide(next_ob[:, 0], 180)
    roll = np.divide(next_ob[:, 1], 180)
    cost_pr = np.power(pitch, 2) + np.power(roll, 2)
    cost_rates = np.power(next_ob[:, 3], 2) + np.power(next_ob[:, 4], 2) + np.power(next_ob[:, 5], 2)
    lambda_omega = .0001
    cost = cost_pr + lambda_omega * cost_rates
    return cost


def push_history(new, orig):
    """
    Takes in the new data and makes it the first elements of a vector.
    - For using dynamics models with history.
    :param new: New data
    :param orig: old data (with some form of history)
    :return: [new, orig] cut at old length
    """
    assert len(orig) / len(new) % 1.0 == 0
    hist = int(len(orig) / len(new))
    l = len(new)
    data = np.copy(orig)
    data[l:] = orig[:-l]
    data[:l] = new
    return data


'''
Some notes on the crazyflie PID structure. Essentially there is a trajectory planner
  that we can ignore, and a Attitude control that sents setpoints to a rate controller.
  The Attitude controller outputs a rate desired, and the rate desired updates motors

This is the code from the fimrware. You can see how the m1...m4 pwm values are set
  The motorPower.m1 is a pwm value, and limit thrust puts it in an range:
    motorPower.m1 = limitThrust(control->thrust + control->pitch +
                               control->yaw);
    motorPower.m2 = limitThrust(control->thrust - control->roll -
                               control->yaw);
    motorPower.m3 =  limitThrust(control->thrust - control->pitch +
                               control->yaw);
    motorPower.m4 =  limitThrust(control->thrust + control->roll -
                               control->yaw);

    This shows that m1 and m3 control pitch while m2 and m4 control roll.
    Yaw should account for a minor amount of this. Our setpoint will be easy,
    roll, pitch =0 ,yaw rate = 0.

Default values, for 250Hz control. Will expect our simulated values to differ:
Axis Mode: [KP, KI, KD, iLimit]

Pitch Rate: [250.0, 500.0, 2.5, 33.3]
Roll Rate: [250.0, 500.0, 2.5, 33.3]
Yaw Rate: [120.0, 16.7, 0.0, 166.7]
Pitch Attitude: [6.0, 3.0, 0.0, 20.0]
Roll Attitude: [6.0, 3.0, 0.0, 20.0]
Yaw Attitude: [6.0, 1.0, 0.35, 360.0]

"the angle PID runs on the fused IMU data to generate a desired rate of rotation. This rate of rotation feeds in to the rate PID which produces motor setpoints"
'''


# def rollout_model(s0, controller):

def smart_model_step(model, state, action):
    states_in = model.state_list
    actions_in = model.input_list
    targets = model.change_state_list
    len_in = len(states_in)
    len_out = len(targets)

    def convert_predictions(prediction, state_in, targets_list):
        output = np.copy(state_in[:len(targets_list)])
        for i, (p, s, t_l) in enumerate(zip(prediction, state_in, targets_list)):
            # if delta, add change
            if t_l[-2:] == 'dx':
                output[i] = s + p
            else:
                output[i] = p
        return output

    if len_out > len_in:
        history_used = True

    if len(action) < len(actions_in):
        if len(actions_in) % len(action) == 0:
            hist = int(len(actions_in) / len(action))
            action = np.array([action] * hist).flatten()
    output, logvars = model.predict(state, action, ret_var=True)
    next_state = convert_predictions(output, state, targets)

    return next_state, torch.exp(logvars)

    # if history_used:
    #     next_state = np.concatenate(next_state, state[len(targets):])


global cfg


######################################################################
@hydra.main(config_path='conf/simulate.yaml')
def optimizer(cfg):
    log.info("============= Configuration =============")
    log.info(f"Config:\n{cfg.pretty()}")
    log.info("=========================================")

    global model
    global sim

    model = torch.load(cwd_basedir() + 'ex_data/models/iono.dat')
    trained_data = pd.read_csv(cwd_basedir() + 'ex_data/SAS/iono.csv')

    states_in = model.state_list
    actions_in = model.input_list
    targets = model.change_state_list

    s = trained_data[states_in].values
    a = trained_data[actions_in].values
    t = trained_data[targets].values

    def get_permissible_states(states):
        # for a dataframe and a model, get some permissible data for initial states for model rollouts

        # Look for data with low pitch and roll
        flag = (abs(states[:, 0]) < 10) & (abs(states[:, 1]) < 10)
        reasonable = states[flag, :]
        return reasonable

    initial_states = get_permissible_states(s)
    val = np.random.randint(0, len(initial_states))
    s0 = initial_states[val]

    def rollout_opttask(params):
        cum_cost = 0
        # for r in range(cfg.bo.rollouts):
        #     val = np.random.randint(0, len(initial_states))
        #     s0 = initial_states[val]
        p = np.array(params)
        pid_params = [[p[0, 0], 0.0, p[0, 1]], [p[0, 2], 0.0, p[0, 3]]]
        sim.policy.set_params(pid_params)
        sim.policy.reset()
        for s0 in initial_states:
            cost = sim.basic_rollout(s0, model)
            cum_cost += cost

        return np.sum(cum_cost).reshape(1, 1)

    sim = BOPID(cfg.bo, cfg.policy, rollout_opttask)
    traj = sim.basic_rollout(s0, model)

    msg = "Initialized BO Objective of PID Control"
    # msg +=
    log.info(msg)
    sim.optimize()
    # sim.opt.plot_optimization_curve()
    logs = sim.getParameters()
    plot_cost_itr(logs, cfg)
    plot_parameters(logs, cfg)
    # from opto.opto.plot import paretoFront
    # paretoFront(logs.data.fx)
    print("\n Other items tried")
    for vals, fx in zip(np.array(logs.data.x.T), np.array(logs.data.fx.T)):
        print(f"Cost - {fx}: Pp: ", vals[0], " Dp: ", vals[1], "Pr: ", vals[2], " Dr: ", vals[3])


def plot_cost_itr(logs, cfg):
    # TODO make a plot with the cost over time of iterations (with minimum)
    import matplotlib.pyplot as plt
    itr = np.arange(0, logs.data.n_evals)
    costs = logs.data.fx
    best = logs.data.opt_fx
    ax = plt.subplot(111)
    cum_min = np.minimum.accumulate(costs.squeeze()) - .02
    plt.step(itr, costs.squeeze(), where='mid', label='Cost at Iteration')  # drawstyle="steps-post",
    plt.step(itr, cum_min, where='mid', label='Best Cost')  # drawstyle="steps-post", l
    plt.legend()
    ax.set_xlabel("Iteration")
    ax.set_ylabel("Weighted, Cumulative Attitude Cost")
    ax.set_ylim([0, 5])
    plt.savefig("costs.pdf")
    return


def plot_parameters(logs, cfg):
    # Returns a plot of the different PID parameters tested,
    # with the P and D plotted separately
    best = np.array(logs.data.opt_x).squeeze()
    samples = np.array(logs.data.x).T
    Kp1 = samples[:, 0]
    Kp2 = samples[:, 2]
    Kd1 = samples[:, 1]
    Kd2 = samples[:, 3]

    import matplotlib.pyplot as plt
    ax1 = plt.subplot(111)
    ax1.scatter(Kp1, Kp2, label='sampled Kp')
    ax1.scatter(best[0], best[2], marker='*', s=130, label='Best Kp')
    ax1.set_ylim([0, cfg.policy.pid.params.max_values[0]])
    ax1.set_xlim([0, cfg.policy.pid.params.max_values[0]])
    ax1.set_xlabel("KP Pitch")
    ax1.set_ylabel("KP Roll")
    plt.legend()
    plt.savefig("KP.pdf")

    ax2 = plt.subplot(111)
    ax2.scatter(Kd1, Kd2, label='sampled Kd')
    ax2.scatter(best[1], best[3], marker='*', s=130, label='Best Kd')
    ax2.set_ylim([0, cfg.policy.pid.params.max_values[1]])
    ax2.set_xlim([0, cfg.policy.pid.params.max_values[1]])
    ax2.set_xlabel("KD Pitch")
    ax2.set_ylabel("KD Roll")
    ax2.legend()
    plt.savefig("KD.pdf")
    return


if __name__ == '__main__':
    sys.exit(optimizer())
