# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved

# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
from __future__ import division
import torch.nn as nn
import torch
import typing
import gym
import threading
from torch import multiprocessing as mp
import logging
import traceback
import os
import numpy as np
import copy
import nle
import minihack
import contextlib
import termios
import tty
import random

from nle import nethack


from src.core import prof
from src.env_utils import FrameStack, Environment
from src import atari_wrappers as atari_wrappers

from gym_minigrid import wrappers as wrappers


def translate_message(x):
    result_string = ""
    for c in x.flatten():
        if c == 0:
            break
        result_string += chr(c)
    return result_string


def untranslate_message(m):
    res = torch.zeros(1, 256).long()
    for i, c in enumerate(m):
        res[0, i] = ord(c)
    return res


def update_goal_dict(goal_dict, message):
    message_text = translate_message(message)
    goal_dict[message_text] = message


def sample_goal(goal_dict):
    return random.choice(list(goal_dict.values()))


@contextlib.contextmanager
def no_echo():
    tt = termios.tcgetattr(0)
    try:
        tty.setraw(0)
        yield
    finally:
        termios.tcsetattr(0, termios.TCSAFLUSH, tt)


def repeat_batch(batch, n_repeats=10):
    batch_rep = {}
    batch_rep["glyphs"] = batch["glyphs"].repeat(n_repeats, 1, 1, 1)
    batch_rep["blstats"] = batch["blstats"].repeat(n_repeats, 1, 1)
    batch_rep["message"] = batch["message"].repeat(n_repeats, 1, 1)
    return batch_rep


# This augmentation is based on random walk of agents
def augmentation(frames):
    # agent_loc = agent_loc(frames)
    return frames


# Entropy loss on categorical distribution
def catentropy(logits):
    a = logits - torch.max(logits, dim=-1, keepdim=True)[0]
    e = torch.exp(a)
    z = torch.sum(e, dim=-1, keepdim=True)
    p = e / z
    entropy = torch.sum(p * (torch.log(z) - a), dim=-1)
    return torch.mean(entropy)


# Here is computing how many objects
def num_objects(frames):
    T, B, H, W, *_ = frames.shape
    num_objects = frames[:, :, :, :, 0]
    num_objects = (
        (num_objects == 4).long()
        + (num_objects == 5).long()
        + (num_objects == 6).long()
        + (num_objects == 7).long()
        + (num_objects == 8).long()
    )
    return num_objects


# EMA of the 2 networks
def soft_update_params(net, target_net, tau):
    for param, target_param in zip(net.parameters(), target_net.parameters()):
        target_param.data.copy_(tau * param.data + (1 - tau) * target_param.data)


shandle = logging.StreamHandler()
shandle.setFormatter(logging.Formatter("[%(levelname)s:%(process)d %(module)s:%(lineno)d %(asctime)s] " "%(message)s"))
log = logging.getLogger("torchbeast")
log.propagate = False
log.addHandler(shandle)
log.setLevel(logging.INFO)

Buffers = typing.Dict[str, typing.List[torch.Tensor]]


def create_env(flags):
    if "MiniHack" in flags.env:
        seed = int.from_bytes(os.urandom(4), byteorder="little")
        kwargs = {}
        kwargs["observation_keys"] = ("glyphs", "blstats", "chars", "message")
        kwargs["savedir"] = None
        env = gym.make(flags.env, **kwargs)
        env.seed(seed)
        return env

    elif "Mario" in flags.env:
        env = atari_wrappers.wrap_pytorch(
            atari_wrappers.wrap_deepmind(
                atari_wrappers.make_atari(flags.env, noop=True),
                clip_rewards=False,
                frame_stack=True,
                scale=False,
                fire=True,
            )
        )
        env = JoypadSpace(env, COMPLETE_MOVEMENT)
        return env
    else:
        env = atari_wrappers.wrap_pytorch(
            atari_wrappers.wrap_deepmind(
                atari_wrappers.make_atari(flags.env, noop=False),
                clip_rewards=False,
                frame_stack=True,
                scale=False,
                fire=False,
            )
        )
        return env


def get_batch(
    free_queue: mp.Queue,
    full_queue: mp.Queue,
    buffers: Buffers,
    initial_agent_state_buffers,
    flags,
    timings,
    lock=threading.Lock(),  # noqa
):
    with lock:
        timings.time("lock")
        indices = [full_queue.get() for _ in range(flags.batch_size)]
        timings.time("dequeue")
    batch = {key: torch.stack([buffers[key][m] for m in indices], dim=1) for key in buffers}
    initial_agent_state = (torch.cat(ts, dim=1) for ts in zip(*[initial_agent_state_buffers[m] for m in indices]))
    timings.time("batch")
    for m in indices:
        free_queue.put(m)
    timings.time("enqueue")
    batch = {k: t.to(device=flags.device, non_blocking=True) for k, t in batch.items()}
    initial_agent_state = tuple(t.to(device=flags.device, non_blocking=True) for t in initial_agent_state)
    timings.time("device")
    return batch, initial_agent_state


def create_heatmap_buffers(obs_shape):
    specs = []
    for r in range(obs_shape[0]):
        for c in range(obs_shape[1]):
            specs.append(tuple([r, c]))
    buffers: Buffers = {key: torch.zeros(1).share_memory_() for key in specs}
    return buffers


def create_buffers(obs_space, num_actions, flags) -> Buffers:
    T = flags.unroll_length

    if type(obs_space) is gym.spaces.dict.Dict:
        size = (flags.unroll_length + 1,)
        # Get specimens to infer shapes and dtypes.
        samples = {k: torch.from_numpy(v) for k, v in obs_space.sample().items()}
        specs = {key: dict(size=size + sample.shape, dtype=sample.dtype) for key, sample in samples.items()}
        specs.update(
            policy_hiddens=dict(size=(T + 1, flags.hidden_dim), dtype=torch.float32),
            reward=dict(size=size, dtype=torch.float32),
            bonus_reward=dict(size=size, dtype=torch.float32),
            bonus_reward2=dict(size=size, dtype=torch.float32),
            goal_reward=dict(size=size, dtype=torch.float32),
            done=dict(size=size, dtype=torch.bool),
            episode_return=dict(size=size, dtype=torch.float32),
            episode_step=dict(size=size, dtype=torch.int32),
            policy_logits=dict(size=size + (num_actions,), dtype=torch.float32),
            episode_state_count=dict(size=(T + 1,), dtype=torch.float32),
            train_state_count=dict(size=(T + 1,), dtype=torch.float32),
            baseline=dict(size=size, dtype=torch.float32),
            last_action=dict(size=size, dtype=torch.int64),
            action=dict(size=size, dtype=torch.int64),
            state_visits=dict(size=size, dtype=torch.int32),
        )
        specs["goal"] = specs["message"].copy()

    else:
        obs_shape = obs_space.shape
        specs = dict(
            frame=dict(size=(T + 1, *obs_shape), dtype=torch.uint8),
            reward=dict(size=(T + 1,), dtype=torch.float32),
            bonus_reward=dict(size=(T + 1,), dtype=torch.float32),
            bonus_reward2=dict(size=(T + 1,), dtype=torch.float32),
            goal_reward=dict(size=(T + 1,), dtype=torch.float32),
            done=dict(size=(T + 1,), dtype=torch.bool),
            episode_return=dict(size=(T + 1,), dtype=torch.float32),
            episode_step=dict(size=(T + 1,), dtype=torch.int32),
            last_action=dict(size=(T + 1,), dtype=torch.int64),
            policy_logits=dict(size=(T + 1, num_actions), dtype=torch.float32),
            baseline=dict(size=(T + 1,), dtype=torch.float32),
            action=dict(size=(T + 1,), dtype=torch.int64),
            episode_win=dict(size=(T + 1,), dtype=torch.int32),
            carried_obj=dict(size=(T + 1,), dtype=torch.int32),
            carried_col=dict(size=(T + 1,), dtype=torch.int32),
            partial_obs=dict(size=(T + 1, 7, 7, 3), dtype=torch.uint8),
            episode_state_count=dict(size=(T + 1,), dtype=torch.float32),
            train_state_count=dict(size=(T + 1,), dtype=torch.float32),
            partial_state_count=dict(size=(T + 1,), dtype=torch.float32),
            encoded_state_count=dict(size=(T + 1,), dtype=torch.float32),
        )
        specs["goal"] = specs["message"].copy()

    buffers: Buffers = {key: [] for key in specs}
    for _ in range(flags.num_buffers):
        for key in buffers:
            buffers[key].append(torch.empty(**specs[key]).share_memory_())
    return buffers


def act(
    i: int,
    free_queue: mp.Queue,
    full_queue: mp.Queue,
    model: torch.nn.Module,
    encoder: torch.nn.Module,
    buffers: Buffers,
    episode_state_count_dict: dict,
    initial_agent_state_buffers,
    flags,
    message_goals,
    goal_learner=None,  # optional
):
    try:
        log.info("Actor %i started.", i)
        timings = prof.Timings()

        gym_env = create_env(flags)
        seed = i ^ int.from_bytes(os.urandom(4), byteorder="little")
        gym_env.seed(seed)

        if flags.num_input_frames > 1:
            gym_env = FrameStack(gym_env, flags.num_input_frames)

        env = Environment(gym_env, fix_seed=flags.fix_seed, env_seed=flags.env_seed)

        env_output = env.initial()
        agent_state = model.initial_state(batch_size=1)

        visited = set()
        current_goal = goal_learner.sample_goal(visited)
        agent_output, unused_state = model(env_output, agent_state, goal=current_goal)
        update_goal_dict(message_goals, env_output["message"][0])
        prev_env_output = None

        rank1_update = True

        if flags.episodic_bonus_type in [
            "elliptical-policy",
            "elliptical-rand",
            "elliptical-icm",
            "elliptical-icm-lifelong",
        ]:
            if rank1_update:
                cov_inverse = torch.eye(flags.hidden_dim) * (1.0 / flags.ridge)
            else:
                cov = torch.eye(flags.hidden_dim) * flags.ridge
            outer_product_buffer = torch.empty(flags.hidden_dim, flags.hidden_dim)

        step = 0

        while True:
            index = free_queue.get()
            if index is None:
                break

            # Write old rollout end.
            for key in env_output:
                buffers[key][index][0, ...] = env_output[key]
            for key in agent_output:
                buffers[key][index][0, ...] = agent_output[key]

            buffers["goal"][index][0, ...] = current_goal

            for i, tensor in enumerate(agent_state):
                initial_agent_state_buffers[index][i][...] = tensor

            if flags.episodic_bonus_type == "counts-obs":
                # full observation: glyph image + stats + message
                episode_state_key = tuple(
                    env_output["glyphs"].view(-1).tolist()
                    + env_output["blstats"].view(-1).tolist()
                    + env_output["message"].view(-1).tolist()
                )
            elif flags.episodic_bonus_type == "counts-msg":
                # message only
                episode_state_key = tuple(env_output["message"].view(-1).tolist())
            elif flags.episodic_bonus_type == "counts-glyphs":
                # glyph image only
                episode_state_key = tuple(env_output["glyphs"].view(-1).tolist())
            elif flags.episodic_bonus_type == "counts-pos":
                # (x, y) position extracted from the stats vector
                episode_state_key = tuple(env_output["blstats"].view(-1).tolist()[:2])
            elif flags.episodic_bonus_type == "counts-img":
                # pixel image (for Vizdoom)
                episode_state_key = tuple(env_output["frame"].contiguous().view(-1).tolist())
            else:
                episode_state_key = ()

            if flags.episodic_bonus_type in [
                "counts-obs",
                "counts-msg",
                "counts-glyphs",
                "counts-pos",
                "counts-img",
            ]:
                if episode_state_key in episode_state_count_dict:
                    episode_state_count_dict[episode_state_key] += 1
                else:
                    episode_state_count_dict.update({episode_state_key: 1})
                buffers["episode_state_count"][index][0, ...] = torch.tensor(
                    1 / np.sqrt(episode_state_count_dict.get(episode_state_key))
                )

            # Reset the episode state counts or (inverse) covariance matrix when the episode is over
            if env_output["done"][0][0]:
                step = 0
                for _episode_state_key in episode_state_count_dict:
                    episode_state_count_dict = dict()
                if flags.episodic_bonus_type in [
                    "elliptical-policy",
                    "elliptical-rand",
                    "elliptical-icm",
                ]:
                    if rank1_update:
                        cov_inverse = torch.eye(flags.hidden_dim) * (1.0 / flags.ridge)
                    else:
                        cov_inverse = torch.eye(flags.hidden_dim) * flags.ridge

            # Do new rollout
            update_goal_dict(message_goals, env_output["message"][0])
            visited.add(translate_message(env_output["message"][0]))

            for t in range(flags.unroll_length):
                timings.reset()

                with torch.no_grad():
                    agent_output, agent_state = model(env_output, agent_state, goal=current_goal)
                    if flags.episodic_bonus_type in [
                        "elliptical-rand",
                        "elliptical-icm",
                        "elliptical-icm-diag",
                        "elliptical-icm-lifelong",
                    ]:
                        encoder_output, encoder_state = encoder(env_output, tuple())

                timings.time("model")

                env_output = env.step(agent_output["action"])
                update_goal_dict(message_goals, env_output["message"][0])
                visited.add(translate_message(env_output["message"][0]))

                timings.time("step")

                for key in env_output:
                    buffers[key][index][t + 1, ...] = env_output[key]

                for key in agent_output:
                    buffers[key][index][t + 1, ...] = agent_output[key]

                buffers["goal"][index][t + 1, ...] = current_goal

                if flags.episodic_bonus_type == "elliptical-policy":
                    # run through policy net to get embeddings
                    h = agent_output["policy_hiddens"].squeeze().detach()
                    u = torch.mv(cov_inverse, h)
                    b = torch.dot(h, u).item()

                    torch.outer(u, u, out=outer_product_buffer)
                    torch.add(
                        cov_inverse,
                        outer_product_buffer,
                        alpha=-(1.0 / (1.0 + b)),
                        out=cov_inverse,
                    )

                    if step == 0:
                        b = 0

                    buffers["bonus_reward"][index][t + 1, ...] = b

                elif flags.episodic_bonus_type in [
                    "elliptical-rand",
                    "elliptical-icm",
                    "elliptical-icm-lifelong",
                ]:
                    # run through encoder to get embeddings
                    h = encoder_output.squeeze().detach()

                    if rank1_update:
                        u = torch.mv(cov_inverse, h)
                        b = torch.dot(h, u).item()

                        torch.outer(u, u, out=outer_product_buffer)
                        torch.add(
                            cov_inverse,
                            outer_product_buffer,
                            alpha=-(1.0 / (1.0 + b)),
                            out=cov_inverse,
                        )
                    else:
                        cov = cov + torch.outer(h, h)
                        cov_inverse = torch.inverse(cov)
                        u = torch.mv(cov_inverse, h)
                        b = torch.dot(h, u).item()

                    if step == 0:
                        b = 0

                    buffers["bonus_reward"][index][t + 1, ...] = b

                elif flags.episodic_bonus_type == "none":
                    buffers["bonus_reward"][index][t + 1, ...] = 0
                else:
                    assert "counts" in flags.episodic_bonus_type

                if (env_output["message"][0] == current_goal).all():
                    goal_reward = 1.0
                    current_goal = goal_learner.sample_goal(visited)
                else:
                    goal_reward = 0.0
                buffers["goal_reward"][index][t + 1, ...] = goal_reward

                step += 1

                if flags.episodic_bonus_type == "counts-obs":
                    episode_state_key = tuple(
                        env_output["glyphs"].view(-1).tolist()
                        + env_output["blstats"].view(-1).tolist()
                        + env_output["message"].view(-1).tolist()
                    )
                elif flags.episodic_bonus_type == "counts-msg":
                    episode_state_key = tuple(env_output["message"].view(-1).tolist())
                elif flags.episodic_bonus_type == "counts-glyphs":
                    episode_state_key = tuple(env_output["glyphs"].view(-1).tolist())
                elif flags.episodic_bonus_type == "counts-pos":
                    episode_state_key = tuple(env_output["blstats"].view(-1).tolist()[:2])
                elif flags.episodic_bonus_type == "counts-img":
                    episode_state_key = tuple(env_output["frame"].contiguous().view(-1).tolist())
                else:
                    episode_state_key = ()

                if flags.episodic_bonus_type in [
                    "counts-obs",
                    "counts-msg",
                    "counts-glyphs",
                    "counts-pos",
                    "counts-img",
                ]:
                    if episode_state_key in episode_state_count_dict:
                        episode_state_count_dict[episode_state_key] += 1
                    else:
                        episode_state_count_dict.update({episode_state_key: 1})
                    buffers["episode_state_count"][index][t + 1, ...] = torch.tensor(
                        1 / np.sqrt(episode_state_count_dict.get(episode_state_key))
                    )

                timings.time("bonus update")
                # Reset the episode state counts/covariance when the episode is over
                if env_output["done"][0][0]:
                    step = 0
                    episode_state_count_dict = dict()
                    if flags.episodic_bonus_type in [
                        "elliptical-policy",
                        "elliptical-rand",
                        "elliptical-icm",
                    ]:
                        if rank1_update:
                            cov_inverse = torch.eye(flags.hidden_dim) * (1.0 / flags.ridge)
                        else:
                            cov = torch.eye(flags.hidden_dim) * flags.ridge
                    visited = set()
                    current_goal = goal_learner.sample_goal(visited)

                timings.time("write")

            full_queue.put(index)

        if i == 0:
            log.info("Actor %i: %s", i, timings.summary())

    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.error("Exception in worker process %i", i)
        traceback.print_exc()
        print()
        raise e


def unroll_with_goal(flags, model, env, goal_target_str, goal_init_str):
    """
    Unrolls the model in the environment to test goal reaching success rate.
    Goal target is the target we're trying to reach
    Goal init str is the goal we give to the model
    """

    env_output = env.initial()
    agent_state = model.initial_state(batch_size=1)

    current_goal = untranslate_message(goal_init_str)
    reach_goal = untranslate_message(goal_target_str)
    agent_output, unused_state = model(env_output, agent_state, goal=current_goal)

    ctr = 0
    outputs = []
    renders = []
    goal_reached = False
    while not (env_output["done"][0][0] and ctr > 0):
        env_output = env.step(agent_output["action"])
        outputs.append(env_output)
        renders.append(env.gym_env.render(mode="ansi"))
        agent_output, agent_state = model(env_output, agent_state, goal=current_goal)
        ctr += 1
        if (env_output["message"] == reach_goal).all():
            goal_reached = True

    return outputs, renders, goal_reached


def test_one_goal(flags, model, env, goal_target_str, goal_init_str, evals=10):
    s = 0
    for _ in range(evals):
        _, _, g = unroll_with_goal(flags, model, env, goal_target_str, goal_init_str)
        if g:
            s += 1
    return s / evals


def run_goal_reaching_eval(flags, model, goal_learner):
    env = create_env(flags)
    if flags.num_input_frames > 1:
        env = FrameStack(env, flags.num_input_frames)
    env = Environment(env)

    results = {}
    goals = goal_learner.graph.managed_message_to_id.keys()

    evals_per_goal = flags.evals_per_goal

    for goal in goals:
        other_goals = set(goals) - set([goal])
        other_goal = random.choice(list(other_goals))
        wrong = test_one_goal(flags, model, env, goal, other_goal, evals_per_goal)
        correct = test_one_goal(flags, model, env, goal, goal, evals_per_goal)
        results[goal] = (correct, wrong)

    return results
