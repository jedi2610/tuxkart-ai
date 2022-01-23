import torch
import numpy as np

from tqdm import tqdm, trange
from collections import deque
from scipy.signal import lfilter
from stable_baselines3.common.vec_env import SubprocVecEnv

from src.utils import get_encoder

class PPOBuffer:
    """
    Buffer to save all the (s, a, r, s`) for each step taken.
    """

    def __init__(self, buffer_size, batch_size, obs_dim, act_dim, num_frames, gamma, lam):
        self.ptr = 0
        self.buffer_size = buffer_size
        self.batch_size = batch_size
        self.num_frames = num_frames
        self.obs_dim = obs_dim
        self.act_dim = act_dim
        self.gamma = gamma
        self.lam = lam
        self.reset()

    def reset(self):
        # an optimization would be to store the dict instead of the array
        self.infos = []
        self.obs = np.zeros((self.buffer_size, self.batch_size, *self.obs_dim[:-1]), dtype=np.float32)
        self.actions = np.zeros((self.buffer_size, self.batch_size, len(self.act_dim)), dtype=np.float32)
        self.rewards = np.zeros((self.buffer_size, self.batch_size), dtype=np.float32)
        self.returns = np.zeros((self.buffer_size, self.batch_size), dtype=np.float32)
        self.values = np.zeros((self.buffer_size+1, self.batch_size), dtype=np.float32)
        self.log_probs = np.zeros((self.buffer_size, self.batch_size), dtype=np.float32)
        self.advantage = np.zeros((self.buffer_size, self.batch_size), dtype=np.float32)

    def save(self, obs, act, reward, value, infos, log_prob):
        self.obs[self.ptr] = obs
        self.actions[self.ptr] = act
        self.rewards[self.ptr] = reward
        self.values[self.ptr] = value
        self.infos.append(infos)
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
        # advantage = discounted sum of rewards - baseline estimate
        # meaning what is the reward i got for taking an action - the reward i was expecting for that action
        # delta = (reward + value of next state) - value of the current state
        # advantage = discounted sum of delta
        # advantage = gae = (current reward + (gamma * value of next state) - value of current state) + (discounted sum of gae)
        # advantage = gae = (current reward + essentially a measure of how better the next state is compared to the current state) + (discounted sum of gae)
        self.values[self.ptr] = next_value
        deltas = self.rewards + self.gamma * self.values[1:] - self.values[:-1]
        self.advantage = self.discounted_sum(deltas, self.gamma * self.lam)     # advantage estimate using GAE
        self.returns = self.discounted_sum(self.rewards, self.gamma)            # discounted sum of rewards
        self.advantage = (self.advantage - np.mean(self.advantage, axis=0)) / (np.std(self.advantage, axis=0) + 1e-6) # axis 0 because advantage is of shape (buffer_size,
        # self.returns = self.advantage - self.values[:-1]                      # some use this, some use the above
        del self.values

    def can_train(self):
        return (self.ptr - self.num_frames - 1) > 0

    def get_ptr(self):
        return self.ptr

    def get(self):
        idx = np.random.randint(low=self.num_frames, high=self.ptr)
        idx_range = slice(idx-self.num_frames, idx)
        return self.obs[idx_range], self.actions[idx], self.infos[idx], self.returns[idx], self.log_probs[idx], self.advantage[idx]


class PPO():

    EPOCHS = 2
    GAMMA = 0.9
    LAMBDA = 0.95
    EPSILON = 0.2
    ENTROPY_BETA = 0.2
    CRITIC_DISCOUNT = 0.5

    def __init__(self, env: SubprocVecEnv, model, optimizer, logger, device, **buffer_args):
        """
        :param env: list of STKEnv or vectorized envs?
        """

        self.env = env
        self.model = model
        self.opt = optimizer
        self.device = device
        self.logger = logger
        self.info_encoder = get_encoder(env.observation_space.shape)
        buffer_args['gamma'], buffer_args['lam'] = self.GAMMA, self.LAMBDA
        self.buffer = PPOBuffer(**buffer_args)
        self.num_frames = buffer_args['num_frames'] + 1
        self.buffer_size = buffer_args['buffer_size']

    def rollout(self):

        prevInfo = [None for _ in range(self.env.num_envs)]
        images = self.env.reset()
        images = deque([np.array(images) for _ in range(self.num_frames)], maxlen=self.num_frames)
        to_numpy = lambda x: x.to(device='cpu').numpy()

        with torch.no_grad():
            for i in trange(self.buffer_size):

                encoded_infos = self.info_encoder(prevInfo)
                images[0] = encoded_infos # basically appending left without popping off the last element from the other side
                obs = torch.from_numpy(np.transpose(np.array(images), (1, 0, 2, 3))).to(self.device)

                dist, value = self.model(obs)
                action = dist.sample()
                log_prob = dist.log_prob(action)
                obs, reward, done, info = self.env.step(to_numpy(action))
                self.buffer.save(images[-1], to_numpy(action), reward,
                        to_numpy(value.squeeze(dim=-1)), prevInfo, to_numpy(log_prob))
                prevInfo = info
                images.append(obs)

                if done.any():
                    break

            print('-------------------------------------------------------------')
            print(f'Trajectory cut off at {i+1} time steps')
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

            encoded_infos = self.info_encoder(prevInfo)
            images[0] = encoded_infos
            obs = torch.from_numpy(np.transpose(np.array(images), (1, 0, 2, 3))).to(self.device)
            _, next_value = self.model(obs)
            self.buffer.compute_gae(to_numpy(next_value.squeeze(dim=1)))

    def train(self):

        to_cuda = lambda x: torch.from_numpy(x).to(device=torch.device(self.device),
            dtype=torch.float32) if isinstance(x, np.ndarray) else x
        if not self.buffer.can_train():
            print("Buffer size is too small")
            return

        for epoch in trange(self.EPOCHS):
            t = tqdm((range(self.buffer.get_ptr())))
            for timestep in t:

                self.opt.zero_grad()
                obs, act, info, returns, logp_old, adv = map(to_cuda, self.buffer.get())
                obs = torch.cat((to_cuda(self.info_encoder(info)).unsqueeze(dim=0), obs), dim=0)
                dist, value_new = self.model(obs.permute(1, 0, 2, 3)) # transpose axes because it is originally in shape (D, N, H, W)
                logp_new = dist.log_prob(act)

                ratio = (logp_new - logp_old).exp()
                surr1 = ratio * adv
                surr2 = torch.clamp(ratio, 1 + self.EPSILON, 1 - self.EPSILON) * adv

                actor_loss = -torch.min(surr1, surr2).mean()
                critic_loss = self.CRITIC_DISCOUNT * ((value_new.squeeze() - returns)**2).mean()
                entropy_loss = self.ENTROPY_BETA * dist.entropy().mean()

                loss = actor_loss + critic_loss + entropy_loss
                loss.backward()
                self.opt.step()

                step = epoch * timestep
                t.set_description(f"loss: {loss}")
                self.logger.log_train(step, actor_loss, critic_loss, entropy_loss, loss)
