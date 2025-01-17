from .controller import Controller
import torch


class MPController(Controller):
    def __init__(self, env, model, controller_cfg):
        self.env = env
        self.model = model
        self.cfg = controller_cfg
        self.N = self.cfg.params.N
        self.T = self.cfg.params.T
        self.hold = self.cfg.params.hold

        self.low = torch.tensor(self.env.action_space.low, dtype=torch.float32)
        self.high = torch.tensor(self.env.action_space.high, dtype=torch.float32)
        self.action_sampler = torch.distributions.Uniform(low=self.low, high=self.high)

    def reset(self):
        print("Resetting MPController Not Needed, but passed")
        return

    def get_action(self, state):
        # repeat the state
        state0 = torch.tensor(state).repeat(self.N, 1)
        states = torch.zeros(self.N, self.T + 1, state.shape[-1])
        states[:, 0, :] = state0
        rewards = torch.zeros(self.N, self.T)
        if self.hold:
            action_candidates = self.action_sampler.sample(sample_shape=(self.N, 1)).repeat(1, self.T, 1)
        else:
            action_candidates = self.action_sampler.sample(sample_shape=(self.N, self.T))

        # TODO
        for t in range(self.T):
            action_batch = action_candidates[:, t, :]
            state_batch = states[:, t, :]
            next_state_batch = torch.tensor(self.model.predict(state_batch, action_batch))
            states[:, t + 1, :] = next_state_batch
            rewards[:, t] = self.env.get_reward_torch(next_state_batch, action_batch)

        # TODO compute rewards
        cumulative_reward = torch.sum(rewards, dim=1)
        best = torch.argmax(cumulative_reward)
        actions_seq = action_candidates[best, :, :]
        best_action = actions_seq[0]
        return best_action
