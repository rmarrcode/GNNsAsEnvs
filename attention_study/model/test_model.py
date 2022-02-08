# rl/ai imports
from ray.rllib.agents import ppo
import numpy as np
import json
import os
import torch
import torch.optim as optim
from tensorboard_logger import Logger as TbLogger
import sys

# our code
from sigma_graph.envs.figure8.figure8_squad_rllib import Figure8SquadRLLib
from attention_study.model.attention_policy import PolicyModel
from attention_study.generate_baseline_metrics import parse_arguments, create_env_config, create_trainer_config
from attention_study.model.utils import embed_obs_in_map, load_edge_dictionary
from sigma_graph.data.file_manager import set_visibility

# 3rd party
from attention_routing.nets.attention_model import AttentionModel, set_decode_type
from attention_routing.problems.tsp.problem_tsp import TSP
from attention_routing.train import train_batch
from attention_routing.utils.log_utils import log_values
from attention_routing.nets.critic_network import CriticNetwork
from attention_routing.options import get_options
from attention_routing.train import train_epoch, validate, get_inner_model
from attention_routing.reinforce_baselines import NoBaseline, ExponentialBaseline, CriticBaseline, RolloutBaseline, WarmupBaseline
from attention_routing.nets.attention_model import AttentionModel
from attention_routing.nets.pointer_network import PointerNetwork, CriticNetworkLSTM
from attention_routing.utils import torch_load_cpu, load_problem
from attention_routing.train import clip_grad_norms


def initialize_train_artifacts(opts):
    '''
    code mostly from attention_routing/run.py:run(opts)
    repurposed for reinforcement learning here.
    :params None
    :returns optimizer, baseline, lr_scheduler, val_dataset, problem, tb_logger, opts
    '''
    # Set the random seed
    torch.manual_seed(opts.seed)

    # Optionally configure tensorboard
    tb_logger = None
    if not opts.no_tensorboard:
        #print(os.path.join(opts.log_dir, "{}_{}".format(opts.problem, opts.graph_size), opts.run_name))
        tb_logger = TbLogger(os.path.join(opts.log_dir, "{}_{}".format(opts.problem, opts.graph_size), opts.run_name))
    
    os.makedirs(opts.save_dir)
    # Save arguments so exact configuration can always be found
    with open(os.path.join(opts.save_dir, "args.json"), 'w') as f:
        json.dump(vars(opts), f, indent=True)

    # Set the device
    opts.device = torch.device("cuda:0" if opts.use_cuda else "cpu")

    # Figure out what's the problem
    problem = load_problem(opts.problem)

    # Load data from load_path
    load_data = {}
    assert opts.load_path is None or opts.resume is None, "Only one of load path and resume can be given"
    load_path = opts.load_path if opts.load_path is not None else opts.resume
    if load_path is not None:
        print('  [*] Loading data from {}'.format(load_path))
        load_data = torch_load_cpu(load_path)

    # Initialize model
    model_class = {
        'attention': AttentionModel,
        'pointer': PointerNetwork
    }.get(opts.model, None)
    assert model_class is not None, "Unknown model: {}".format(model_class)
    model = model_class(
        opts.embedding_dim,
        opts.hidden_dim,
        problem,
        n_encode_layers=opts.n_encode_layers,
        mask_inner=True,
        mask_logits=True,
        normalization=opts.normalization,
        tanh_clipping=opts.tanh_clipping,
        checkpoint_encoder=opts.checkpoint_encoder,
        shrink_size=opts.shrink_size
    ).to(opts.device)

    if opts.use_cuda and torch.cuda.device_count() > 1:
        model = torch.nn.DataParallel(model)

    # Overwrite model parameters by parameters to load
    model_ = get_inner_model(model)
    model_.load_state_dict({**model_.state_dict(), **load_data.get('model', {})})

    # Initialize baseline
    if opts.baseline == 'exponential':
        baseline = ExponentialBaseline(opts.exp_beta)
    elif opts.baseline == 'critic' or opts.baseline == 'critic_lstm':
        assert problem.NAME == 'tsp', "Critic only supported for TSP"
        baseline = CriticBaseline(
            (
                CriticNetworkLSTM(
                    2,
                    opts.embedding_dim,
                    opts.hidden_dim,
                    opts.n_encode_layers,
                    opts.tanh_clipping
                )
                if opts.baseline == 'critic_lstm'
                else
                CriticNetwork(
                    2,
                    opts.embedding_dim,
                    opts.hidden_dim,
                    opts.n_encode_layers,
                    opts.normalization
                )
            ).to(opts.device)
        )
    elif opts.baseline == 'rollout':
        baseline = RolloutBaseline(model, problem, opts)
    else:
        assert opts.baseline is None, "Unknown baseline: {}".format(opts.baseline)
        baseline = NoBaseline()

    if opts.bl_warmup_epochs > 0:
        baseline = WarmupBaseline(baseline, opts.bl_warmup_epochs, warmup_exp_beta=opts.exp_beta)

    # Load baseline from data, make sure script is called with same type of baseline
    if 'baseline' in load_data:
        baseline.load_state_dict(load_data['baseline'])

    # Initialize optimizer
    optimizer = optim.Adam(
        [{'params': model.parameters(), 'lr': opts.lr_model}]
        + (
            [{'params': baseline.get_learnable_parameters(), 'lr': opts.lr_critic}]
            if len(baseline.get_learnable_parameters()) > 0
            else []
        )
    )

    # Load optimizer state
    if 'optimizer' in load_data:
        optimizer.load_state_dict(load_data['optimizer'])
        for state in optimizer.state.values():
            for k, v in state.items():
                # if isinstance(v, torch.Tensor):
                if torch.is_tensor(v):
                    state[k] = v.to(opts.device)

    # Initialize learning rate scheduler, decay by lr_decay once per epoch!
    lr_scheduler = optim.lr_scheduler.LambdaLR(optimizer, lambda epoch: opts.lr_decay ** epoch)

    if opts.resume:
        epoch_resume = int(os.path.splitext(os.path.split(opts.resume)[-1])[0].split("-")[1])

        torch.set_rng_state(load_data['rng_state'])
        if opts.use_cuda:
            torch.cuda.set_rng_state_all(load_data['cuda_rng_state'])
        # Set the random states
        # Dumping of state was done before epoch callback, so do that now (model is loaded)
        baseline.epoch_callback(model, epoch_resume)
        print("Resuming after {}".format(epoch_resume))
        opts.epoch_start = epoch_resume + 1
    
    return model, optimizer, baseline, lr_scheduler, tb_logger


TEST_SETTINGS = {
    'is_standalone': True, # are we training it in rllib, or standalone?
    'is_360_view': True, # can the agent see in all directions at once?
    'is_obs_embedded': False, # are our observations embedded into the graph?
    'is_per_step': False, # do we optimize every step if True, or every episode if False?
    'is_mask_in_model': False, # do we use the mask in the model or after the model?
    'use_hardcoded_bl': True, # subtract off a hardcoded "baseline" value
}
MAXIMUM_THEORETICAL_REWARD = 25
def get_cost_from_reward(reward):
    return 1/(reward + 1e-3) # takes care of div by 0

def optimize(optimizer, baseline, reward, ll):
    # set costs
    model_cost = get_cost_from_reward(reward)
    bl_val, bl_loss = baseline.eval(attention_input, model_cost) #if bl_val is None else (bl_val, 0) # critic loss
    if TEST_SETTINGS['use_hardcoded_bl']:
        bl_val = get_cost_from_reward(MAXIMUM_THEORETICAL_REWARD)
    reinforce_loss = ((model_cost - bl_val) * ll).mean()
    loss = reinforce_loss + bl_loss
    # perform optimization step
    optimizer.zero_grad()
    loss.backward()
    grad_norms = clip_grad_norms(optimizer.param_groups, opts.max_grad_norm)
    optimizer.step()
    return grad_norms, loss

if __name__ == "__main__":
    # init config
    print('creating config')
    parser = parse_arguments()
    config = parser.parse_args()
    outer_configs, n_episodes = create_env_config(config)
    set_visibility(TEST_SETTINGS['is_360_view'])
    
    # train model standalone
    if TEST_SETTINGS['is_standalone']:
        # init model and optimizer
        opts = get_options()
        model, optimizer, baseline, lr_scheduler, tb_logger =\
            initialize_train_artifacts(opts)
        model.train()
        set_decode_type(model, "sampling")
        
        # init training env
        training_env = Figure8SquadRLLib(outer_configs)
        acs_edges_dict = load_edge_dictionary(training_env.map.g_acs.adj)
        obs = [[0] * np.product(training_env.observation_space.shape)]
        if not TEST_SETTINGS['is_obs_embedded']:
            attention_input = embed_obs_in_map(obs, training_env.map) # can be done here if not embedded
        
        # training loop
        print('training')
        episode_length = 20
        num_training_episodes = 10000
        total_reward = 0
        total_ll = None
        logged_reward = 0 # for logging
        logged_ll = None
        for episode in range(num_training_episodes):
            training_env.reset();
            agent_node = training_env.team_red[training_env.learning_agent[0]].agent_node # 1-indexed value
            for step in range(episode_length):
                # get model predictions
                if TEST_SETTINGS['is_obs_embedded']:
                    attention_input = embed_obs_in_map(obs, training_env.map) # embed obs every time we get a new obs
                if TEST_SETTINGS['is_mask_in_model']:
                    cost, ll, log_ps = model(attention_input, acs_edges_dict, [agent_node-1], return_log_p=True)
                else:
                    cost, ll, log_ps = model(attention_input, return_log_p=True)
                # mask model predictions with our graph edges
                if not TEST_SETTINGS['is_mask_in_model']:
                    node_exclude_list = np.array(list(range(attention_input.shape[1])))
                    mask = [np.delete(node_exclude_list, list(acs_edges_dict[agent_node])) for agent_node in [agent_node-1]]
                    for i in range(len(mask)):
                        log_ps[i][mask[i]] = 0
                # move_action decoding. get max prob moves from masked predictions
                features = log_ps # set features for value branch later
                transformed_features = features.clone()
                transformed_features[transformed_features == 0] = -float('inf')
                optimal_destination = torch.argmax(transformed_features, dim=1)
                curr_loc = agent_node
                next_loc = optimal_destination[0].item() + 1
                move_action = training_env.map.g_acs.adj[curr_loc][next_loc]['action']
                look_action = 1 # TODO!!!!!!!! currently uses all-way look
                action = Figure8SquadRLLib.convert_multidiscrete_action_to_discrete(move_action, look_action)
                # step through environment to update obs/rew and agent node
                actions = {}
                for a in training_env.learning_agent:
                    actions[str(a)] = action
                obs, rew, done, _ = training_env.step(actions)
                agent_node = training_env.team_red[training_env.learning_agent[0]].agent_node
                # collect rewards/losses
                for a in training_env.learning_agent:
                    total_reward += rew[str(a)]
                if not total_ll:
                    total_ll = ll
                else:
                    total_ll += ll
                # optimize once per step
                if TEST_SETTINGS['is_per_step']:
                    grad_norms, loss = optimize(optimizer, baseline, total_reward, total_ll)
                    # reset for next iteration
                    logged_reward += total_reward
                    logged_ll = total_ll if not logged_ll else logged_ll + total_ll
                    total_reward = 0
                    total_ll = None
                # end episode if simulation is done
                if done['__all__']:
                    break
            
            # optimize once per episode
            if not TEST_SETTINGS['is_per_step']:
                grad_norms, loss = optimize(optimizer, baseline, total_reward / episode_length, total_ll / episode_length)
                # reset for next iteration
                logged_reward = total_reward
                logged_ll = total_ll
                total_reward = 0
                total_ll = None
            
            # log results
            print('reward', logged_reward)
            # log step in tb for metrics
            #if episode % int(opts.log_step) == 0:
            if episode % 10 == 0:
                logged_reward = torch.tensor(logged_reward, dtype=torch.float32)
                log_values(logged_reward, grad_norms, episode, episode, episode,
                        logged_ll, loss, 0, tb_logger, opts, mode="reward")
            logged_reward = 0
            logged_ll = None
    else:
        # create model
        ppo_trainer = ppo.PPOTrainer(config=create_trainer_config(outer_configs, trainer_type=ppo, custom_model=True), env=Figure8SquadRLLib)
        print('trainer created')
        # test model
        ppo_trainer.train()
        print('model trained')
