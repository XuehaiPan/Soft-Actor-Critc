import itertools

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from common.collector import Collector
from common.utils import clone_network, sync_params
from sac.network import SoftQNetwork, PolicyNetwork, StateEncoderWrapper


__all__ = ['ModelBase', 'Trainer', 'Tester']


class ModelBase(object):
    def __init__(self, env, state_encoder, state_dim, action_dim, hidden_dims, activation,
                 initial_alpha, n_samplers, buffer_capacity, devices, random_seed=0):
        self.devices = itertools.cycle(devices)
        self.model_device = next(self.devices)

        self.state_dim = state_dim
        self.action_dim = action_dim

        self.training = True

        self.state_encoder = StateEncoderWrapper(state_encoder, device=self.model_device)

        self.soft_q_net_1 = SoftQNetwork(state_dim, action_dim, hidden_dims,
                                         activation=activation, device=self.model_device)
        self.soft_q_net_2 = SoftQNetwork(state_dim, action_dim, hidden_dims,
                                         activation=activation, device=self.model_device)
        self.policy_net = PolicyNetwork(state_dim, action_dim, hidden_dims, activation=activation, device=self.model_device)

        self.log_alpha = nn.Parameter(torch.tensor([[np.log(initial_alpha)]], dtype=torch.float32, device=self.model_device),
                                      requires_grad=True)

        self.modules = nn.ModuleDict([
            ('state_encoder', self.state_encoder),
            ('soft_q_net_1', self.soft_q_net_1),
            ('soft_q_net_2', self.soft_q_net_2),
            ('policy_net', self.policy_net),
            ('params', nn.ParameterDict({'log_alpha': self.log_alpha}))
        ])
        self.modules.share_memory()

        self.collector = Collector(state_encoder=self.state_encoder,
                                   policy_net=self.policy_net,
                                   env=env,
                                   buffer_capacity=buffer_capacity,
                                   n_samplers=n_samplers,
                                   devices=self.devices,
                                   random_seed=random_seed)

    def print_info(self):
        print(f'env = {self.env}')
        print(f'state_dim = {self.state_dim}')
        print(f'action_dim = {self.action_dim}')
        print(f'device = {self.model_device}')
        print(f'buffer_capacity = {self.replay_buffer.capacity}')
        print(f'n_samplers = {self.collector.n_samplers}')
        print(f'sampler_devices = {list(map(str, self.collector.devices))}')
        print('Modules:', self.modules)

    @property
    def env(self):
        return self.collector.env

    @property
    def replay_buffer(self):
        return self.collector.replay_buffer

    def sample(self, n_episodes, max_episode_steps, deterministic=False, random_sample=False,
               render=False, log_dir=None):
        self.collector.sample(n_episodes, max_episode_steps, deterministic, random_sample,
                              render, log_dir)

    def async_sample(self, n_episodes, max_episode_steps, deterministic=False, random_sample=False,
                     render=False, log_dir=None):
        samplers = self.collector.async_sample(n_episodes, max_episode_steps,
                                               deterministic, random_sample,
                                               render, log_dir)
        return samplers

    def train(self, mode=True):
        self.training = mode
        self.modules.train(mode=mode)
        self.collector.train(mode=mode)
        return self

    def eval(self):
        return self.train(mode=False)

    def save_model(self, path):
        torch.save(self.modules.state_dict(), path)

    def load_model(self, path):
        self.modules.load_state_dict(torch.load(path, map_location=self.model_device))


class Trainer(ModelBase):
    def __init__(self, env, state_encoder, state_dim, action_dim, hidden_dims, activation,
                 initial_alpha, soft_q_lr, policy_lr, alpha_lr, weight_decay,
                 n_samplers, buffer_capacity, devices, random_seed=None):
        super().__init__(env, state_encoder, state_dim, action_dim, hidden_dims, activation,
                         initial_alpha, n_samplers, buffer_capacity, devices, random_seed)

        self.target_soft_q_net_1 = clone_network(src_net=self.soft_q_net_1, device=self.model_device)
        self.target_soft_q_net_2 = clone_network(src_net=self.soft_q_net_2, device=self.model_device)
        self.target_soft_q_net_1.eval()
        self.target_soft_q_net_2.eval()

        self.soft_q_criterion_1 = nn.MSELoss()
        self.soft_q_criterion_2 = nn.MSELoss()

        self.optimizer = optim.Adam(itertools.chain(self.state_encoder.parameters(),
                                                    self.soft_q_net_1.parameters(),
                                                    self.soft_q_net_2.parameters(),
                                                    self.policy_net.parameters()),
                                    lr=soft_q_lr, weight_decay=weight_decay)
        self.policy_loss_weight = policy_lr / soft_q_lr
        self.alpha_optimizer = optim.Adam([self.log_alpha], lr=alpha_lr)

        self.train(mode=True)

    def update(self, batch_size, normalize_rewards=True, reward_scale=1.0,
               adaptive_entropy=True, target_entropy=-2.0,
               gamma=0.99, soft_tau=0.01, epsilon=1E-6):
        self.train()

        # size: (batch_size, item_size)
        observation, action, reward, next_observation, done = tuple(map(lambda tensor: tensor.to(self.model_device),
                                                                        self.replay_buffer.sample(batch_size)))

        state = self.state_encoder(observation)
        with torch.no_grad():
            next_state = self.state_encoder(next_observation)

        # Normalize rewards
        if normalize_rewards:
            with torch.no_grad():
                reward = reward_scale * (reward - reward.mean()) / (reward.std() + epsilon)

        # Update temperature parameter
        new_action, log_prob = self.policy_net.evaluate(state)
        if adaptive_entropy is True:
            alpha_loss = -(self.log_alpha * (log_prob + target_entropy).detach()).mean()
            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()
        with torch.no_grad():
            alpha = self.log_alpha.exp()

        # Train Q function
        predicted_q_value_1 = self.soft_q_net_1(state, action)
        predicted_q_value_2 = self.soft_q_net_2(state, action)
        with torch.no_grad():
            new_next_action, next_log_prob = self.policy_net.evaluate(next_state)

            target_q_min = torch.min(self.target_soft_q_net_1(next_state, new_next_action),
                                     self.target_soft_q_net_2(next_state, new_next_action))
            target_q_min -= alpha * next_log_prob
            target_q_value = reward + (1 - done) * gamma * target_q_min
        soft_q_loss_1 = self.soft_q_criterion_1(predicted_q_value_1, target_q_value)
        soft_q_loss_2 = self.soft_q_criterion_2(predicted_q_value_2, target_q_value)
        soft_q_loss = (soft_q_loss_1 + soft_q_loss_2) / 2.0

        # Train policy function
        predicted_new_q_value = torch.min(self.soft_q_net_1(state, new_action),
                                          self.soft_q_net_2(state, new_action))
        policy_loss = (alpha * log_prob - predicted_new_q_value).mean()

        loss = soft_q_loss + self.policy_loss_weight * policy_loss
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        # Soft update the target value net
        sync_params(src_net=self.soft_q_net_1, dst_net=self.target_soft_q_net_1, soft_tau=soft_tau)
        sync_params(src_net=self.soft_q_net_2, dst_net=self.target_soft_q_net_2, soft_tau=soft_tau)

        info = {
            'action_scale': (self.soft_q_net_1.action_scale + self.soft_q_net_2.action_scale) / 2.0
        }
        return soft_q_loss.item(), policy_loss.item(), alpha.item(), info

    def load_model(self, path):
        super().load_model(path=path)
        self.target_soft_q_net_1.load_state_dict(self.soft_q_net_1.state_dict())
        self.target_soft_q_net_2.load_state_dict(self.soft_q_net_2.state_dict())
        self.target_soft_q_net_1.eval()
        self.target_soft_q_net_2.eval()


class Tester(ModelBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.eval()