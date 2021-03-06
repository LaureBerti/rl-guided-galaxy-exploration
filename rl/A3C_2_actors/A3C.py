from scipy.stats.stats import mode
import wandb
import tensorflow as tf
from tensorflow.keras.layers import Input, Dense, LSTM
import json
import os
from datetime import datetime
import gym
import argparse
import numpy as np
from threading import Thread, Lock
from multiprocessing import cpu_count
import random

from app.pipelines.pipeline import Pipeline
from app.pipelines.pipeline_onfly_sets import PipelineWithOnFlySets
from app.pipelines.pipeline_precalculated_sets import \
    PipelineWithPrecalculatedSets

from .pipeline_environment import PipelineEnvironment
from .set_actor import SetActor
from .operation_actor import OperationActor
from .critic import Critic
from .intrinsic_curiosity_model import IntrinsicCuriosityForwardModel

tf.keras.backend.set_floatx('float64')

now = datetime.now()
parser = argparse.ArgumentParser()
parser.add_argument('--gamma', type=float, default=0.99)
parser.add_argument('--update_interval', type=int, default=50)
parser.add_argument('--actor_lr', type=float, default=0.00002)
parser.add_argument('--critic_lr', type=float, default=0.00008)
parser.add_argument('--icm_lr', type=float, default=0.05)
parser.add_argument('--workers', type=int, default=6)
parser.add_argument('--lstm_steps', type=int, default=10)
parser.add_argument('--target_set', type=str, default=None)
parser.add_argument('--notes', type=str, default="")
parser.add_argument('--mode', type=str, default="scattered")
parser.add_argument('--curiosity_ratio', type=float, default="0")
parser.add_argument('--counter_curiosity_ratio', type=float, default="0")
parser.add_argument('--name', type=str,
                    default=now.strftime("%m-%d-%Y_%H:%M:%S"))

args = parser.parse_args()
if args.curiosity_ratio != 0:
    args.name = f"{args.mode}-icm-{args.curiosity_ratio}-lstm-{args.lstm_steps}-alr-{args.actor_lr}-clr-{args.critic_lr}-icmlr-{args.icm_lr}-{now.strftime('%m%d%Y_%H%M%S')}"
elif args.counter_curiosity_ratio != 0:
    args.name = f"{args.mode}-ccur-{args.counter_curiosity_ratio}-lstm-{args.lstm_steps}-alr-{args.actor_lr}-clr-{args.critic_lr}-{now.strftime('%m%d%Y_%H%M%S')}"
else:
    args.name = f"{args.mode}-no-curiosity-{now.strftime('%m%d%Y_%H%M%S')}"

wandb.init(name=args.name, project="deep-rl-tf2", config={
    "gamma": args.gamma,
    "update_interval": args.update_interval,
    "actor_lr": args.actor_lr,
    "critic_lr": args.critic_lr,
    "icm_lr": args.icm_lr,
    "workers": args.workers,
    "lstm_steps": args.lstm_steps,
    "target_set": args.target_set,
    "notes": args.notes,
    "mode": args.mode,
    "curiosity_ratio": args.curiosity_ratio,
    "counter_curiosity_ratio": args.counter_curiosity_ratio
})
CUR_EPISODE = 0

class Agent:
    def __init__(self, env_name, pipeline=None):
        self.pipeline = pipeline
        self.steps = args.lstm_steps
        self.episode_steps = 500
        self.agent_name=args.name
        self.env = PipelineEnvironment(
            self.pipeline,  target_set_name=args.target_set, mode=args.mode, episode_steps=self.episode_steps)
        self.env_name = env_name

        self.set_state_dim = self.env.set_state_dim
        self.operation_state_dim = self.env.operation_state_dim

        self.set_action_dim = self.env.set_action_space.n
        self.operation_action_dim = self.env.operation_action_space.n
        self.global_set_actor = SetActor(
            self.set_state_dim, self.set_action_dim, self.steps, args.actor_lr,self.agent_name)
        self.global_operation_actor = OperationActor(
            self.operation_state_dim, self.operation_action_dim, self.steps, args.actor_lr, self.agent_name)
        self.global_critic = Critic(self.set_state_dim, self.steps, args.critic_lr, self.agent_name)
        self.curiosity_module = IntrinsicCuriosityForwardModel(
            self.operation_state_dim+1, self.set_state_dim, 16, args.icm_lr, self.agent_name)
        self.num_workers = args.workers  # cpu_count()
        self.set_op_counters = {}

    def train(self, max_episodes=10000):
        workers = []

        for i in range(self.num_workers):
            env = PipelineEnvironment(
                self.pipeline, target_set_name=args.target_set, mode=args.mode, agentId=i, episode_steps=self.episode_steps, target_items=self.env.state_encoder.initial_target_items)

            workers.append(WorkerAgent(
                env, self.global_set_actor, self.global_operation_actor, self.global_critic, max_episodes, self.curiosity_module, self.set_op_counters, agentId=i, episode_steps=self.episode_steps))

        for worker in workers:
            worker.start()

        for worker in workers:
            worker.join()

        self.global_operation_actor.save_model()
        self.global_set_actor.save_model()
        self.global_critic.save_model()


class WorkerAgent(Thread):
    def __init__(self, env: PipelineEnvironment, global_set_actor: SetActor, global_operation_actor: OperationActor, global_critic: Critic, max_episodes, global_curiosity_module: IntrinsicCuriosityForwardModel, global_set_op_counters, agentId=-1, episode_steps=50):
        Thread.__init__(self)
        self.lock = Lock()
        self.env = env
        self.agentId = agentId
        self.set_state_dim = env.set_state_dim
        self.operation_state_dim = env.operation_state_dim
        self.steps = global_set_actor.steps
        self.set_action_dim = env.set_action_space.n
        self.operation_action_dim = env.operation_action_space.n
        self.global_set_op_counters = global_set_op_counters
        self.max_episodes = max_episodes
        self.global_set_actor = global_set_actor
        self.global_operation_actor = global_operation_actor
        self.global_critic = global_critic
        self.episode_steps = episode_steps
        self.set_actor = SetActor(
            self.set_state_dim, self.set_action_dim, self.steps, self.global_set_actor.lr, self.global_set_actor.agent_name)
        self.operation_actor = OperationActor(
            self.operation_state_dim, self.operation_action_dim, self.steps, self.global_operation_actor.lr, self.global_operation_actor.agent_name)
        self.critic = Critic(self.set_state_dim, self.steps, self.global_critic.lr, self.global_critic.agent_name)
        self.target_max_curiosity_reward = 100
        self.counter_curiosity_factor = self.target_max_curiosity_reward/self.episode_steps
        if args.curiosity_ratio > 0:
            self.global_curiosity_module = global_curiosity_module
            self.curiosity_module = IntrinsicCuriosityForwardModel(
                global_curiosity_module.prediction_input_state_dim, global_curiosity_module.target_input_state_dim, global_curiosity_module.output_dim)

        self.set_actor.model.set_weights(
            self.global_set_actor.model.get_weights())
        self.operation_actor.model.set_weights(
            self.global_operation_actor.model.get_weights())
        self.critic.model.set_weights(self.global_critic.model.get_weights())

    def n_step_td_target(self, rewards, next_v_value, done):
        td_targets = np.zeros_like(rewards)
        cumulative = 0
        if not done:
            cumulative = next_v_value

        for k in reversed(range(0, len(rewards))):
            cumulative = args.gamma * cumulative + rewards[k]
            td_targets[k] = cumulative
        return td_targets

    def advantage(self, td_targets, baselines):
        return td_targets - baselines

    def list_to_batch(self, list):
        batch = list[0]
        for elem in list[1:]:
            batch = np.append(batch, elem, axis=0)
        return batch

    def train(self):
        operation_episodes = 0
        set_episodes = 0
        global CUR_EPISODE
        systemRandom = random.SystemRandom()
        while self.max_episodes >= CUR_EPISODE:
            set_state_batch = []
            operation_state_batch = []
            set_action_batch = []
            operation_action_batch = []
            reward_batch = []
            icm_states_batch = []
            icm_ground_truth_batch = []
            episode_set_op_counters = {}
            episode_reward = 0
            episode_loss = 0
            episode_total_op_counters = 0
            episode_extrinsic_reward = 0
            episode_intrinsic_reward = 0
            done = False
            set_action_steps = [[-1] * self.set_state_dim] * self.steps
            operation_action_steps = [
                [-1] * self.operation_state_dim] * self.steps
            set_state = self.env.reset()
            set_action_steps.pop(0)
            set_action_steps.append(set_state)
            while not done:
                probs = self.set_actor.model.predict(
                    np.array(set_action_steps).reshape((1, self.steps, self.set_state_dim)))
                probs = self.env.fix_possible_set_action_probs(probs[0])
                if all(np.isnan(x) for x in probs):
                    set_action = 0
                else:
                    set_action = np.random.choice(
                        self.set_action_dim, p=probs)
                operation_state = self.env.get_operation_state(set_action)
                operation_action_steps.pop(0)
                operation_action_steps.append(operation_state)
                probs = self.operation_actor.model.predict(
                    np.array(operation_action_steps).reshape((1, self.steps, self.operation_state_dim)))
                probs = self.env.fix_possible_operation_action_probs(
                    probs[0], set_action)
                if np.isnan(probs[0]):
                    operation_action = self.env.get_random_operation(
                        set_action)
                else:
                    operation_action = np.random.choice(
                        self.operation_action_dim, p=probs)

                next_set_state, reward, done, set_op_pair = self.env.step(
                    set_action, operation_action)

                if set_op_pair in episode_set_op_counters:
                    episode_set_op_counters[set_op_pair] += 1
                else:
                    episode_set_op_counters[set_op_pair] = 1
                next_set_action_steps = set_action_steps.copy()
                next_set_action_steps.pop(0)
                next_set_action_steps.append(next_set_state)
                if set_op_pair in self.global_set_op_counters:
                    op_counter = episode_set_op_counters[set_op_pair] + \
                        self.global_set_op_counters[set_op_pair]
                else:
                    op_counter = episode_set_op_counters[set_op_pair]
                episode_total_op_counters += op_counter
                if args.curiosity_ratio > 0:
                    icm_state = np.concatenate(
                        ([operation_action], operation_state))

                    loss = self.curiosity_module.get_loss(np.reshape(icm_state, [1, self.operation_state_dim+1]),
                                                          np.reshape(next_set_state, [1, self.set_state_dim]))
                    if loss > 1000000:
                        intrinsic_reward = 1
                    else:
                        intrinsic_reward = loss/1000000
                    episode_intrinsic_reward += float(intrinsic_reward)

                    print(
                        f"Agent: {self.agentId} Op counter:{op_counter} Loss:{loss} Intrisic reward: {intrinsic_reward}")
                    episode_extrinsic_reward += reward
                    reward = args.curiosity_ratio * \
                        float(intrinsic_reward) + \
                        (1-args.curiosity_ratio) * reward
                    icm_states_batch.append(np.reshape(
                        icm_state, [1, self.operation_state_dim+1]))
                    icm_ground_truth_batch.append(np.reshape(
                        next_set_state, [1, self.set_state_dim]))
                    episode_loss += loss
                elif args.counter_curiosity_ratio > 0:
                    episode_extrinsic_reward += reward
                    intrinsic_reward = self.counter_curiosity_factor/op_counter
                    episode_intrinsic_reward += float(intrinsic_reward)
                    print(
                        f"Agent: {self.agentId} Op counter:{op_counter} Intrisic reward: {intrinsic_reward}")
                    reward = args.counter_curiosity_ratio * \
                        float(intrinsic_reward) + \
                        (1-args.counter_curiosity_ratio) * reward
                else:
                    episode_extrinsic_reward += reward
                reward = np.reshape(reward, [1, 1])
                reward_batch.append(reward)

                operation_action = np.reshape(
                    operation_action, [1, 1])
                set_action = np.reshape(set_action, [1, 1])
                set_state_batch.append(
                    np.array(set_action_steps).reshape((1, self.steps, self.set_state_dim)))
                set_action_batch.append(set_action)
                operation_state_batch.append(np.array(operation_action_steps).reshape(
                    (1, self.steps, self.operation_state_dim)))
                operation_action_batch.append(operation_action)

                if len(set_state_batch) >= args.update_interval or done:
                    set_states = self.list_to_batch(set_state_batch)
                    set_actions = self.list_to_batch(set_action_batch)
                    operation_states = self.list_to_batch(
                        operation_state_batch)
                    operation_actions = self.list_to_batch(
                        operation_action_batch)
                    rewards = self.list_to_batch(reward_batch)

                    next_v_value = self.critic.model.predict(
                        np.array(next_set_action_steps).reshape((1, self.steps, self.set_state_dim)))
                    td_targets = self.n_step_td_target(
                        rewards, next_v_value, done)
                    advantages = td_targets - \
                        self.critic.model.predict(set_states)

                    with self.lock:
                        set_actor_loss = self.global_set_actor.train(
                            set_states, set_actions, advantages)
                        operation_actor_loss = self.global_operation_actor.train(
                            operation_states, operation_actions, advantages)
                        critic_loss = self.global_critic.train(
                            set_states, td_targets)

                        self.set_actor.model.set_weights(
                            self.global_set_actor.model.get_weights())
                        self.operation_actor.model.set_weights(
                            self.global_operation_actor.model.get_weights())
                        self.critic.model.set_weights(
                            self.global_critic.model.get_weights())

                        if args.curiosity_ratio > 0:
                            icm_states = self.list_to_batch(icm_states_batch)
                            icm_ground_truths = self.list_to_batch(
                                icm_ground_truth_batch)
                            self.global_curiosity_module.train(
                                icm_states, icm_ground_truths)
                            self.curiosity_module.prediction_model.set_weights(
                                self.global_curiosity_module.prediction_model.get_weights())

                        for set_op_pair in episode_set_op_counters:
                            if set_op_pair in self.global_set_op_counters:
                                self.global_set_op_counters[set_op_pair] += episode_set_op_counters[set_op_pair]
                            else:
                                self.global_set_op_counters[set_op_pair] = episode_set_op_counters[set_op_pair]

                    set_state_batch = []
                    operation_state_batch = []
                    set_action_batch = []
                    operation_action_batch = []
                    reward_batch = []
                    icm_ground_truth_batch = []
                    icm_states_batch = []

                episode_reward += reward[0][0]
                set_action_steps = next_set_action_steps

            print('EP{} Agent{} EpisodeReward={}'.format(
                CUR_EPISODE, self.agentId, episode_reward))
            log = {
                'reward': episode_reward,
                'sets_viewed': len(self.env.sets_viewed),
                'sets_reviewed': self.env.set_review_counter,
                'min_target_found_set_size_ratio': min(self.env.state_encoder.ratio_item_dict.values()) if len(self.env.state_encoder.ratio_item_dict) > 0 else 0,
                'max_target_found_set_size_ratio': max(self.env.state_encoder.ratio_item_dict.values()) if len(self.env.state_encoder.ratio_item_dict) > 0 else 0,
                'avg_target_found_set_size_ratio': sum(self.env.state_encoder.ratio_item_dict.values())/len(self.env.state_encoder.ratio_item_dict) if len(self.env.state_encoder.ratio_item_dict) > 0 else 0,
                'item_found_ratio': len(self.env.state_encoder.ratio_item_dict)/len(self.env.state_encoder.initial_target_items),
                'extrinsic_reward': episode_extrinsic_reward,
                'intrisic_reward': episode_intrinsic_reward,
                'avg_op_counter': episode_total_op_counters/self.episode_steps,
                **self.env.operation_counter
            }
            if args.curiosity_ratio > 0:
                log['avg_loss'] = episode_loss/self.episode_steps
            wandb.log(log)

            with self.lock:
                CUR_EPISODE += 1
                if CUR_EPISODE % 50 == 0:
                    ep = CUR_EPISODE
                    self.global_operation_actor.save_model(step=ep)
                    self.global_set_actor.save_model(step=ep)
                    self.global_critic.save_model(step=ep)

    def run(self):
        self.train()
