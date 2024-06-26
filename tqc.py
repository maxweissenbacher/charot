import pickle
import time
import hydra
import torch
import torch.cuda
import numpy as np
from tensordict import TensorDict
from torchrl.envs.utils import ExplorationType, set_exploration_type
import wandb
import matplotlib.pyplot as plt
from utils.experiment_name import generate_exp_name
from agents_tqc import (
    log_metrics_offline,
    log_metrics_wandb,
    make_collector,
    make_loss_module,
    make_replay_buffer,
    make_tqc_agent,
    make_tqc_optimizer,
    make_ks_env,
)
from utils.rng import env_seed
from utils.plot import contourplot_KS


@hydra.main(version_base="1.1", config_path="", config_name="config")
def main(cfg: "DictConfig"):  # noqa: F821
    LOGGING_TO_CONSOLE = False
    LOGGING_WANDB = True
    PLOT_TO_WANDB = bool(cfg.logger.plot_to_wandb)

    # Create logger
    exp_name = generate_exp_name(cfg)
    logs = {}
    if LOGGING_WANDB:
        if cfg.logger.project_name is None:
            raise ValueError("WandB project name must be specified in config.")
        wandb.init(
            mode=str(cfg.logger.mode),
            project=str(cfg.logger.project_name),
            name=exp_name,
            config=dict(cfg),
        )

    print('Starting experiment ' + exp_name)

    torch.manual_seed(env_seed(cfg))
    np.random.seed(env_seed(cfg))

    # Create environments
    train_env, eval_env = make_ks_env(cfg)

    # Create agent
    model, exploration_policy = make_tqc_agent(cfg, train_env, eval_env)

    # Create SAC loss
    loss_module, target_net_updater = make_loss_module(cfg, model)

    # Create off-policy collector
    collector = make_collector(cfg, train_env, exploration_policy)

    # Create replay buffer
    replay_buffer = make_replay_buffer(cfg)

    # Create optimizers
    (
        optimizer_actor,
        optimizer_critic,
        optimizer_alpha,
    ) = make_tqc_optimizer(cfg, loss_module)

    # Main loop
    start_time = time.time()
    collected_frames = 0
    # pbar = tqdm.tqdm(total=cfg.collector.total_frames // cfg.env.frame_skip)
    num_console_updates = 1000

    init_random_frames = cfg.collector.init_random_frames // cfg.env.frame_skip
    num_updates = int(
        cfg.collector.env_per_collector
        * (cfg.collector.frames_per_batch // cfg.env.frame_skip)
        * cfg.optim.utd_ratio
    )
    prb = cfg.replay_buffer.prb
    eval_iter = cfg.logger.eval_iter // cfg.env.frame_skip
    frames_per_batch = cfg.collector.frames_per_batch // cfg.env.frame_skip
    eval_rollout_steps = cfg.env.max_episode_steps_eval // cfg.env.frame_skip

    print('Starting sampling and training...')

    sampling_start = time.time()
    train_start_time = sampling_start
    for i, tensordict in enumerate(collector):

        sampling_time = time.time() - sampling_start
        # Update weights of the inference policy
        collector.update_policy_weights_()

        tensordict = tensordict.reshape(-1)
        current_frames = tensordict.numel()
        # Add to replay buffer
        replay_buffer.extend(tensordict.cpu())
        collected_frames += current_frames

        # Console update
        # pbar.update(tensordict.numel())
        if collected_frames % (cfg.collector.total_frames // (cfg.env.frame_skip * num_console_updates)) == 0:
            console_output = f'Frame {collected_frames}/{cfg.collector.total_frames // cfg.env.frame_skip}'
            time_passed = time.time() - train_start_time
            console_output += f' | {time_passed / 60:.0f} min' if time_passed / 60 > 1 else f' | <1 min'
            print(console_output)

        # Optimization steps
        training_start = time.time()
        if collected_frames >= init_random_frames:

            # print(f'\n stop at iteration {i} training commences \n')

            losses = TensorDict(
                {},
                batch_size=[
                    num_updates,
                ],
            )
            for i in range(num_updates):
                # Sample from replay buffer
                sampled_tensordict = replay_buffer.sample().clone()

                # Compute loss
                loss_td = loss_module(sampled_tensordict)

                actor_loss = loss_td["loss_actor"]
                q_loss = loss_td["loss_critic"]
                alpha_loss = loss_td["loss_alpha"]

                # Update actor
                optimizer_actor.zero_grad()
                # For LSTM architecture, need retain_graph=True
                retain_graph = cfg.network.architecture == 'lstm' or cfg.network.architecture == 'buffer_lstm'
                actor_loss.backward(retain_graph=retain_graph)
                optimizer_actor.step()

                # Update critic
                optimizer_critic.zero_grad()
                q_loss.backward()
                optimizer_critic.step()

                # Update alpha
                optimizer_alpha.zero_grad()
                alpha_loss.backward()
                optimizer_alpha.step()

                losses[i] = loss_td.select(
                    "loss_actor", "loss_critic", "loss_alpha"
                ).detach()

                # Update qnet_target params
                target_net_updater.step()

                # Update priority
                if prb:
                    replay_buffer.update_tensordict_priority(sampled_tensordict)

        training_time = time.time() - training_start
        episode_end = (
            tensordict["next", "done"]
            if tensordict["next", "done"].any()
            else tensordict["next", "truncated"]
        )
        episode_rewards = tensordict["next", "episode_reward"][episode_end]

        # Logging
        metrics_to_log = {}
        if len(episode_rewards) > 0:
            episode_length = tensordict["next", "step_count"][episode_end]
            metrics_to_log["train/reward"] = episode_rewards.mean().item() / episode_length.item()
            metrics_to_log["train/last_reward"] = tensordict["next", "reward"][episode_end].item()
            metrics_to_log["train/episode_length"] = cfg.env.frame_skip * episode_length.sum().item() / len(
                episode_length)
        if collected_frames >= init_random_frames:
            metrics_to_log["train/q_loss"] = losses.get("loss_critic").mean().item()
            metrics_to_log["train/actor_loss"] = losses.get("loss_actor").mean().item()
            metrics_to_log["train/alpha_loss"] = losses.get("loss_alpha").mean().item()
            metrics_to_log["train/alpha"] = loss_td["alpha"].item()
            metrics_to_log["train/entropy"] = loss_td["entropy"].item()
            metrics_to_log["train/sampling_time"] = sampling_time
            metrics_to_log["train/training_time"] = training_time

        # Evaluation
        if abs(collected_frames % eval_iter) < frames_per_batch:
            with set_exploration_type(ExplorationType.MODE), torch.no_grad():
                eval_start = time.time()
                eval_rollout = eval_env.rollout(
                    eval_rollout_steps,
                    model[0],
                    auto_cast_to_device=True,
                    break_when_any_done=True,
                )
                eval_time = time.time() - eval_start
                # Compute total reward (norm of solution + norm of actuation)
                eval_reward = eval_rollout["next", "reward"].mean(-2).mean().item()
                last_reward = eval_rollout["next", "reward"][..., -1, :].mean().item()
                # Compute u component of reward
                eval_reward_u = - torch.linalg.norm(eval_rollout["next", "u"], dim=-1).mean(-1).mean().item()
                last_reward_u = - torch.linalg.norm(eval_rollout["next", "u"][..., -1, :], dim=-1).mean().item()
                # Compute mean and std of actuation
                mean_actuation = torch.linalg.norm(eval_rollout["action"], dim=-1).mean(-1).mean().item()
                std_actuation = torch.linalg.norm(eval_rollout["action"], dim=-1).std(-1).mean().item()

                metrics_to_log["eval/reward"] = eval_reward
                metrics_to_log["eval/reward_solution"] = eval_reward_u
                metrics_to_log["eval/last_reward"] = last_reward
                metrics_to_log["eval/last_reward_solution"] = last_reward_u
                metrics_to_log["eval/mean_actuation"] = mean_actuation
                metrics_to_log["eval/std_actuation"] = std_actuation
                metrics_to_log["eval/time"] = eval_time

                # Plot and save to wandb
                if PLOT_TO_WANDB:
                    uu = eval_rollout["next", "u"]
                    contourplot_KS(uu)
                    wandb.log({"eval/plot": wandb.Image(plt)})
                    plt.close()

        if LOGGING_TO_CONSOLE:
            log_metrics_offline(logs, metrics_to_log)
        if LOGGING_WANDB:
            log_metrics_wandb(metrics=metrics_to_log, step=collected_frames * cfg.env.frame_skip)

        sampling_start = time.time()

    collector.shutdown()

    # Save logs to file
    if LOGGING_TO_CONSOLE:
        desc_string = '_' + cfg.logger.filename if cfg.logger.filename is not None else ''
        filename = 'logs' + desc_string + f'_NU00{100 * cfg.env.nu:.0f}_A{cfg.env.num_actuators}_S{cfg.env.num_sensors}.pkl'
        with open(filename, "wb") as f:
            pickle.dump(logs, f)
        print('Saved logs to ' + filename)
    if LOGGING_WANDB:
        wandb.finish()

    end_time = time.time()
    execution_time = end_time - start_time
    print(f"Training took {execution_time:.2f} seconds to finish")


if __name__ == "__main__":
    main()
