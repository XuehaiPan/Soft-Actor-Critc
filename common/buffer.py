import random

import numpy as np
import torch
import torch.multiprocessing as mp

from common.network_base import LSTMHidden


__all__ = ['ReplayBuffer', 'TrajectoryReplayBuffer']


class ReplayBuffer(object):
    def __init__(self, capacity=None, initializer=list, Value=mp.Value, Lock=mp.Lock):
        self.capacity = (capacity or np.inf)
        self.buffer = initializer()
        self.buffer_offset = Value('i', value=0)
        self.lock = Lock()

    def push(self, *args):
        item = tuple(args)
        with self.lock:
            if self.size < self.capacity:
                self.buffer.append(item)
            else:
                self.buffer[self.offset] = item
            self.offset += 1
            if not np.isinf(self.capacity):
                self.offset %= self.capacity

    def extend(self, trajectory):
        with self.lock:
            for items in map(tuple, trajectory):
                if self.size < self.capacity:
                    self.buffer.append(items)
                else:
                    self.buffer[self.offset] = items
                self.offset += 1
                if not np.isinf(self.capacity):
                    self.offset %= self.capacity

    def sample(self, batch_size):
        batch = []
        for i in np.random.randint(self.size, size=batch_size):
            batch.append(self.buffer[i])

        # size: (batch_size, item_size)
        # observation, action, reward, next_observation, done
        return tuple(map(torch.FloatTensor, map(np.stack, zip(*batch))))

    def __len__(self):
        return self.size

    @property
    def size(self):
        return len(self.buffer)

    @property
    def offset(self):
        return self.buffer_offset.get()

    @offset.setter
    def offset(self, value):
        self.buffer_offset.set(value=value)


class TrajectoryReplayBuffer(ReplayBuffer):
    def __init__(self, capacity=None, initializer=list, lock=mp.Lock()):
        super().__init__(capacity=capacity, initializer=initializer, lock=lock)
        self.lengths = initializer()

    def push(self, *args):
        length = len(args[0])
        with self.lock:
            if self.size + length <= self.capacity:
                self.buffer.append(tuple(args))
                self.lengths.append(length)
                self.offset += 1
            else:
                self.buffer[self.offset] = tuple(args)
                self.lengths[self.offset] = length
                self.offset = (self.offset + 1) % (len(self.buffer))

    def sample(self, batch_size, step_size):
        batch = []
        hiddens = []
        lengths = np.asanyarray(list(self.lengths))
        weights = lengths / lengths.sum()
        for i in range(batch_size):
            while True:
                index = np.random.choice(len(weights), p=weights)
                observation, action, reward, next_observation, done, hidden = self.buffer[index]
                if len(observation) >= step_size:
                    offset = random.randint(0, len(observation) - step_size)
                    batch.append((observation[offset:offset + step_size],
                                  action[offset:offset + step_size],
                                  reward[offset:offset + step_size],
                                  next_observation[offset:offset + step_size],
                                  done[offset:offset + step_size]))
                    hiddens.append(hidden[offset].unsqueeze(dim=0))
                    break

        # size: (batch_size, seq_len, item_size)
        # observation, action, reward, next_observation, done
        batch = map(torch.FloatTensor, map(np.stack, zip(*batch)))

        # size: (seq_len, batch_size, item_size)
        observation, action, reward, next_observation, done = tuple(map(lambda tensor: tensor.transpose(0, 1), batch))
        hidden = LSTMHidden.cat(hiddens=hiddens, dim=1)
        return observation, action, reward, next_observation, done, hidden

    @property
    def size(self):
        return sum(self.lengths)
