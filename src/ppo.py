import torch
import numpy as np

from tqdm import tqdm, trange
from collections import deque
import torch.nn.functional as F
from scipy.signal import lfilter
from stable_baselines3.common.vec_env import SubprocVecEnv

from .utils import get_encoder


class PPOBuffer:
    """
    Buffer to store all the (s, a, r, s`) for each step taken.
    """

    def __init__(self, buf_size, num_envs, zdim, act_dim, num_frames, gamma, lam):
        self.ptr = 0
        self.buf_size = buf_size
        self.num_frames = num_frames
        self.num_envs = num_envs
        self.act_dim = act_dim
        self.gamma = gamma
        self.zdim = zdim
        self.lam = lam
        self.calculated_gae = False
        self.reset()

    def reset(self):
        self.obs = np.zeros((self.buf_size, self.num_envs, self.zdim), dtype=np.float32)
        self.actions = np.zeros((self.buf_size, self.num_envs, len(self.act_dim)), dtype=np.float32)
        self.rewards = np.zeros((self.buf_size, self.num_envs), dtype=np.float32)
        self.returns = np.zeros((self.buf_size, self.num_envs), dtype=np.float32)
        self.values = np.zeros((self.buf_size + 1, self.num_envs), dtype=np.float32)
        self.log_probs = np.zeros((self.buf_size, self.num_envs), dtype=np.float32)
        self.advantage = np.zeros((self.buf_size, self.num_envs), dtype=np.float32)

    def save(self, obs, act, reward, value, log_prob):
        self.obs[self.ptr] = obs
        self.actions[self.ptr] = act
        self.rewards[self.ptr] = reward
        self.values[self.ptr] = value
        self.log_probs[self.ptr] = log_prob
        self.ptr += 1

    def discounted_sum(self, x, discount):
        """
        https://github.com/openai/spinningup/blob/master/spinup/algos/pytorch/ppo/core.py#L29
        input:
            [[x0, x1, x2]
             [y0, y1, y2]]
        output:
            [[x0 + discount * x1 + discount^2 * x2, x1 + discount * x2, x2]
             [y0 + discount * y1 + discount^2 * y2, y1 + discount * y2, y2]]
        """
        return np.flip(lfilter([1], [1, -discount], np.flip(x, axis=0), axis=0), axis=0)

    def compute_gae(self, next_value):
        # https://www.reddit.com/r/reinforcementlearning/comments/s18hjr/comment/hs7i2pa
        # https://www.reddit.com/r/reinforcementlearning/comments/sa6hho/why_do_we_need_value_networks
        self.values[self.ptr] = next_value
        deltas = self.rewards + self.gamma * self.values[1:] - self.values[:-1]
        self.advantage = self.discounted_sum(deltas, self.gamma * self.lam)
        self.returns = self.discounted_sum(self.rewards, self.gamma)
        self.advantage = (self.advantage - self.advantage.mean(axis=0)) / (
            self.advantage.std(axis=0) + 1e-5
        )
        self.var_returns_val = np.mean(np.var(self.returns - self.values[:-1], axis=1))
        self.mean_val = np.mean(np.sum(self.values, axis=1))
        self.calculated_gae = True
        del self.values

    def can_train(self):
        return (self.ptr - self.num_frames - 1) > 0

    def get_stats(self):
        assert self.calculated_gae, "Calculate GAE before calling this function"
        return (
            np.mean(self.rewards),
            np.mean(self.returns),
            np.mean(self.advantage),
            self.mean_val,
            self.var_returns_val / np.var(self.returns),
        )

    def get_ptr(self):
        return self.ptr

    def get(self):
        # i could prolly increase the batch_size by collapsing the 0th and 1st axis of the whole
        # buffer, but then it would bring in way too many dim changes all over the place, the code
        # would get really messy everywhere.
        idx = np.random.randint(low=self.num_frames, high=self.ptr)
        idx_range = slice(idx - self.num_frames, idx)
        return (
            idx,
            self.obs[idx_range],
            self.actions[idx],
            self.returns[idx],
            self.log_probs[idx],
            self.advantage[idx],
        )


class PPO:

    EPOCHS = 2
    GAMMA = 0.9
    LAMBDA = 0.95
    EPSILON = 0.2
    ENTROPY_BETA = 0.2
    CRITIC_DISCOUNT = 0.5

    def __init__(self, env: SubprocVecEnv, vae, lstm, optimizer, logger, device, **buffer_args):
        """
        :param env: list of STKEnv or vectorized envs?
        """

        self.env = env
        self.vae = vae
        self.lstm = lstm
        self.opt = optimizer
        self.device = device
        self.logger = logger
        self.info_encoder = get_encoder()
        buffer_args['gamma'], buffer_args['lam'] = self.GAMMA, self.LAMBDA
        self.buffer = PPOBuffer(**buffer_args)
        self.num_frames = buffer_args['num_frames']
        self.zdim, self.buf_size = buffer_args['zdim'], buffer_args['buf_size']

    @torch.no_grad()
    def rollout(self):

        self.vae.eval()
        self.lstm.eval()
        obs = torch.from_numpy(np.array(self.env.reset())).unsqueeze(dim=1).to(self.device)
        prev_info = self.info_encoder(self.env.env_method('get_info'))
        latent_repr = deque(
            np.zeros((self.num_frames, self.env.num_envs, self.zdim), dtype=np.float32),
            maxlen=self.num_frames,
        )
        step = 0
        latent_repr.append(np.column_stack((self.vae.encode(obs)[0].cpu().numpy(), prev_info)))

        def to_numpy(x):
            return x.to(device='cpu').numpy()

        for step in trange(self.buf_size):

            dist, value = self.lstm(torch.from_numpy(np.array(latent_repr)).to(self.device))
            action = dist.sample()
            log_prob = dist.log_prob(action)

            obs, reward, done, info = self.env.step(to_numpy(action))
            obs = torch.from_numpy(np.array(obs)).unsqueeze(dim=1).to(self.device)
            prev_info = self.info_encoder(info)

            latent_repr.append(np.column_stack((self.vae.encode(obs)[0].cpu().numpy(), prev_info)))
            self.buffer.save(
                latent_repr[-1],
                to_numpy(action),
                reward,
                to_numpy(value.squeeze(dim=-1)),
                to_numpy(log_prob),
            )

            self.logger.log_rollout_step(np.mean(reward), value.detach().cpu().mean())
            if done.any():
                break

        print('-------------------------------------------------------------')
        print(f'Trajectory cut off at {step+1} time steps')
        env_infos = np.array(self.env.env_method('get_env_info'))
        race_infos = np.array(info)

        for env_info, race_info in zip(env_infos, race_infos):
            for key, value in env_info.items():
                print(f'{key}: {value}')
            print(f'done: {race_info["done"]}')
            print(f'velocity: {race_info["velocity"]}')
            print(f'overall_distance: {race_info["overall_distance"]}')
            print()
        print('-------------------------------------------------------------\n')

        _, next_value = self.lstm(torch.from_numpy(np.array(latent_repr)).to(self.device))
        self.buffer.compute_gae(to_numpy(next_value.squeeze(dim=-1)))
        avg_rewards, avg_returns, avg_adv, avg_val, residual_var = self.buffer.get_stats()
        self.logger.log_rollout(step, avg_rewards, avg_returns, avg_adv, avg_val, residual_var)

    def train(self):

        self.vae.train()
        self.lstm.train()
        to_cuda = (
            lambda x: torch.from_numpy(x).to(device=torch.device(self.device), dtype=torch.float32)
            if isinstance(x, np.ndarray)
            else x
        )
        if not self.buffer.can_train():
            print("Buffer size is too small")
            return

        for epoch in trange(self.EPOCHS):
            t = tqdm((range(self.buffer.get_ptr() // self.env.num_envs)))
            for timestep in t:

                self.opt.zero_grad()
                idx, latent_repr, act, returns, logp_old, adv = map(to_cuda, self.buffer.get())
                dist, value_new = self.lstm(latent_repr, idx)
                logp_new = dist.log_prob(act)

                ratio = (logp_new - logp_old).exp()
                surr1 = ratio * adv
                surr2 = torch.clamp(ratio, 1 + self.EPSILON, 1 - self.EPSILON) * adv

                actor_loss = -torch.min(surr1, surr2).mean()
                critic_loss = self.CRITIC_DISCOUNT * ((value_new.squeeze() - returns) ** 2).mean()
                entropy_loss = self.ENTROPY_BETA * dist.entropy().mean()

                loss = actor_loss + critic_loss - entropy_loss
                loss.backward()
                self.opt.step()

                kl_div = F.kl_div(logp_old, logp_new, log_target=True, reduction='batchmean')

                t.set_description(f"loss: {loss}")
                self.logger.log_train(
                    actor_loss.item(),
                    critic_loss.item(),
                    entropy_loss.item(),
                    loss.item(),
                    kl_div.item(),
                )
