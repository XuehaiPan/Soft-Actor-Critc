import os
import time
from collections import OrderedDict

import numpy as np
import tqdm
from setproctitle import setproctitle
from torch.utils.tensorboard import SummaryWriter


def train_loop(model, config, update_kwargs):
    with SummaryWriter(log_dir=os.path.join(config.log_dir, 'trainer'), comment='trainer') as writer:
        n_initial_samples = model.collector.n_total_steps
        while model.collector.n_total_steps == n_initial_samples:
            time.sleep(0.1)

        setproctitle(title='trainer')
        for epoch in range(config.initial_epoch + 1, config.n_epochs + 1):
            epoch_soft_q_loss = 0.0
            epoch_policy_loss = 0.0
            epoch_alpha = 0.0
            with tqdm.trange(config.n_updates, desc=f'Training {epoch}/{config.n_epochs}') as pbar:
                for i in pbar:
                    soft_q_loss, policy_loss, alpha, info = model.update(**update_kwargs)

                    buffer_size = model.replay_buffer.size
                    try:
                        update_sample_ratio = (config.n_samples_per_update * model.global_step) / \
                                              (model.collector.n_total_steps - n_initial_samples)
                    except ZeroDivisionError:
                        update_sample_ratio = config.update_sample_ratio
                    epoch_soft_q_loss += (soft_q_loss - epoch_soft_q_loss) / (i + 1)
                    epoch_policy_loss += (policy_loss - epoch_policy_loss) / (i + 1)
                    epoch_alpha += (alpha - epoch_alpha) / (i + 1)
                    writer.add_scalar(tag='train/soft_q_loss', scalar_value=soft_q_loss,
                                      global_step=model.global_step)
                    writer.add_scalar(tag='train/policy_loss', scalar_value=policy_loss,
                                      global_step=model.global_step)
                    writer.add_scalar(tag='train/temperature_parameter', scalar_value=alpha,
                                      global_step=model.global_step)
                    writer.add_scalar(tag='train/buffer_size', scalar_value=buffer_size,
                                      global_step=model.global_step)
                    writer.add_scalar(tag='train/update_sample_ratio', scalar_value=update_sample_ratio,
                                      global_step=model.global_step)
                    pbar.set_postfix(OrderedDict([('global_step', model.global_step),
                                                  ('soft_q_loss', epoch_soft_q_loss),
                                                  ('policy_loss', epoch_policy_loss),
                                                  ('n_samples', f'{model.collector.n_total_steps:.2E}'),
                                                  ('update/sample', f'{update_sample_ratio:.1f}')]))
                    if update_sample_ratio < config.update_sample_ratio:
                        model.collector.pause()
                    else:
                        model.collector.resume()

            writer.add_scalar(tag='epoch/soft_q_loss', scalar_value=epoch_soft_q_loss, global_step=epoch)
            writer.add_scalar(tag='epoch/policy_loss', scalar_value=epoch_policy_loss, global_step=epoch)
            writer.add_scalar(tag='epoch/temperature_parameter', scalar_value=epoch_alpha, global_step=epoch)

            writer.add_figure(tag='epoch/action_scaler_1',
                              figure=model.soft_q_net_1.action_scaler.plot(),
                              global_step=epoch)
            writer.add_figure(tag='epoch/action_scaler_2',
                              figure=model.soft_q_net_2.action_scaler.plot(),
                              global_step=epoch)

            writer.flush()
            if epoch % 10 == 0:
                model.save_model(path=os.path.join(config.checkpoint_dir, f'checkpoint-{epoch}.pkl'))


def train(model, config):
    update_kwargs = config.build_from_keys(['batch_size',
                                            'normalize_rewards',
                                            'reward_scale',
                                            'adaptive_entropy',
                                            'gamma',
                                            'soft_tau'])
    update_kwargs.update(target_entropy=-1.0 * config.action_dim)

    print(f'Start parallel sampling using {config.n_samplers} samplers '
          f'at {tuple(map(str, model.collector.devices))}.')

    model.collector.eval()
    while model.replay_buffer.size < 10 * config.n_samples_per_update:
        model.sample(n_episodes=10,
                     max_episode_steps=config.max_episode_steps,
                     deterministic=False,
                     random_sample=config.RNN_encoder,
                     render=config.render)

    model.collector.train()
    samplers = model.async_sample(n_episodes=np.inf,
                                  deterministic=False,
                                  random_sample=False,
                                  **config.build_from_keys(['max_episode_steps',
                                                            'render',
                                                            'log_episode_video',
                                                            'log_dir']))

    try:
        train_loop(model, config, update_kwargs)
    except KeyboardInterrupt:
        pass
    except Exception:
        raise
    finally:
        for sampler in samplers:
            if sampler.is_alive():
                sampler.terminate()
            sampler.join()


def test(model, config):
    with SummaryWriter(log_dir=config.log_dir) as writer:
        print(f'Start parallel sampling using {config.n_samplers} samplers '
              f'at {tuple(map(str, model.collector.devices))}.')

        model.sample(random_sample=False,
                     **config.build_from_keys([
                         'n_episodes',
                         'max_episode_steps',
                         'deterministic',
                         'render',
                         'log_episode_video',
                         'log_dir'
                     ]))

        episode_steps = np.asanyarray(model.collector.episode_steps)
        episode_rewards = np.asanyarray(model.collector.episode_rewards)
        average_reward = episode_rewards / episode_steps
        writer.add_histogram(tag='test/cumulative_reward', values=episode_rewards)
        writer.add_histogram(tag='test/average_reward', values=average_reward)
        writer.add_histogram(tag='test/episode_steps', values=episode_steps)

        results = {
            'Metrics': ['Cumulative Reward', 'Average Reward', 'Episode Steps'],
            'Mean': list(map(np.mean, [episode_rewards, average_reward, episode_steps])),
            'Stddev': list(map(np.std, [episode_rewards, average_reward, episode_steps])),
        }
        try:
            import pandas as pd
            df = pd.DataFrame(results)
            print(df.to_string(index=False))
        except ImportError:
            for metric, mean, stddev in zip(results['Metrics'], results['Mean'], results['Stddev']):
                print(f'{metric}: {dict(mean=mean, stddev=stddev)}')