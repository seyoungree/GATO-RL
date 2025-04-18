import uuid
import math
import numpy as np
import torch
import time

class RL_AC:
    def __init__(self, env, NN, conf, N_try):
        '''    
        :input env :                            (Environment instance)

        :input conf :                           (Configuration file)

            :parma critic_type :                (str) Activation function to use for the critic NN
            :param LR_SCHEDULE :                (bool) Flag to use a scheduler for the learning rates
            :param boundaries_schedule_LR_C :   (list) Boudaries of critic LR
            :param values_schedule_LR_C :       (list) Values of critic LR
            :param boundaries_schedule_LR_A :   (list) Boudaries of actor LR
            :param values_schedule_LR_A :       (list) Values of actor LR
            :param CRITIC_LEARNING_RATE :       (float) Learning rate for the critic network
            :param ACTOR_LEARNING_RATE :        (float) Learning rate for the policy network
            :param fresh_factor :               (float) Refresh factor
            :param prioritized_replay_alpha :   (float) α determines how much prioritization is used
            :param prioritized_replay_eps :     (float) It's a small positive constant that prevents the edge-case of transitions not being revisited once their error is zero
            :param UPDATE_LOOPS :               (int array) Number of updates of both critic and actor performed every EP_UPDATE episodes
            :param save_interval :              (int) save NNs interval
            :param env_RL :                     (bool) Flag RL environment
            :param nb_state :                   (int) State size (robot state size + 1)
            :param nb_action :                  (int) Action size (robot action size)
            :param MC :                         (bool) Flag to use MC or TD(n)
            :param nsteps_TD_N :                (int) Number of lookahed steps if TD(n) is used
            :param UPDATE_RATE :                (float) Homotopy rate to update the target critic network if TD(n) is used
            :param cost_weights_terminal :      (float array) Running cost weights vector
            :param cost_weights_running :       (float array) Terminal cost weights vector 
            :param dt :                         (float) Timestep
            :param REPLAY_SIZE :                (int) Max number of transitions to store in the buffer. When the buffer overflows the old memories are dropped
            :param NNs_path :                   (str) NNs path
            :param NSTEPS :                     (int) Max episode length

    '''
        self.env = env
        self.NN = NN
        self.conf = conf

        self.N_try = N_try

        self.actor_model = None
        self.critic_model = None
        self.target_critic = None
        self.actor_optimizer = None
        self.critic_optimizer = None

        self.init_rand_state = None
        self.NSTEPS_SH = 0
        self.control_arr = None
        self.state_arr = None
        self.ee_pos_arr = None
        self.exp_counter = np.zeros(self.conf.REPLAY_SIZE)
        return
    
    def setup_model(self, recover_training=None, weights=None):
        ''' Setup RL model '''
        # Create actor, critic and target NNs
        critic_funcs = {
            'elu': self.NN.create_critic_elu,
            'sine': self.NN.create_critic_sine,
            'sine-elu': self.NN.create_critic_sine_elu,
            'relu': self.NN.create_critic_relu
        }
        if weights is not None:
            self.actor_model = self.NN.create_actor(weights = weights[0])
            self.critic_model = critic_funcs[self.conf.critic_type](weights = weights[1])
            self.target_critic = critic_funcs[self.conf.critic_type](weights = weights[2])
        else:
            self.actor_model = self.NN.create_actor()
            self.critic_model = critic_funcs[self.conf.critic_type]()
            self.target_critic = critic_funcs[self.conf.critic_type]()

        # Initialize optimizers
        self.critic_optimizer   = torch.optim.Adam(self.critic_model.parameters(), eps = 1e-7, lr = self.conf.CRITIC_LEARNING_RATE)
        self.actor_optimizer    = torch.optim.Adam(self.actor_model.parameters(), eps = 1e-7, lr = self.conf.ACTOR_LEARNING_RATE)
        # Set lr schedulers
        if self.conf.LR_SCHEDULE:
            # Piecewise constant decay schedule
            #NOTE: not sure about epochs used in 'milestones' variable
            self.CRITIC_LR_SCHEDULE = torch.optim.lr_scheduler.MultiStepLR(self.critic_optimizer, milestones = self.conf.values_schedule_LR_C, gamma = 0.5)
            self.ACTOR_LR_SCHEDULE  = torch.optim.lr_scheduler.MultiStepLR(self.actor_optimizer, milestones = self.conf.values_schedule_LR_A, gamma = 0.5)

        # Set initial weights of the NNs
        if recover_training is not None: 
            #NOTE: this was not tested
            NNs_path_rec = str(recover_training[0])
            N_try = recover_training[1]
            update_step_counter = recover_training[2]   
            self.actor_model.load_state_dict(torch.load(f"{NNs_path_rec}/N_try_{N_try}/actor_{update_step_counter}.pth"))
            self.critic_model.load_state_dict(torch.load(f"{NNs_path_rec}/N_try_{N_try}/critic_{update_step_counter}.pth"))
            self.target_critic.load_state_dict(torch.load(f"{NNs_path_rec}/N_try_{N_try}/target_critic_{update_step_counter}.pth"))
        else:
            self.target_critic.load_state_dict(self.critic_model.state_dict())   

    def update(self, state_batch, state_next_rollout_batch, partial_reward_to_go_batch, d_batch, term_batch, weights_batch, batch_size=None):
        ''' Update both critic and actor '''

        # Update the critic by backpropagating the gradients
        self.critic_optimizer.zero_grad()
        reward_to_go_batch, critic_value, target_critic_value = self.NN.compute_critic_grad(self.critic_model, self.target_critic, state_batch, state_next_rollout_batch, partial_reward_to_go_batch, d_batch, weights_batch)
        self.critic_optimizer.step()  # Update the weights
        
        # Update the actor by backpropagating the gradients
        self.actor_optimizer.zero_grad()
        self.NN.compute_actor_grad(self.actor_model, self.critic_model, state_batch, term_batch, batch_size)

        self.actor_optimizer.step()  # Update the weights
        if self.conf.LR_SCHEDULE:
            self.ACTOR_LR_SCHEDULE.step()
            self.CRITIC_LR_SCHEDULE.step()

        return reward_to_go_batch, critic_value, target_critic_value
        
    def update_target(self, target_weights, weights):
        ''' Update target critic NN '''
        tau = self.conf.UPDATE_RATE
        with torch.no_grad():
            for target_param, param in zip(target_weights, weights):
                target_param.data.copy_(param.data * tau + target_param.data * (1 - tau))

    def learn_and_update(self, update_step_counter, buffer, ep):
        #Tested Successfully# Although only for one iteration (?)
        ''' Sample experience and update buffer priorities and NNs '''
        times_sample = np.zeros(int(self.conf.UPDATE_LOOPS[ep]))
        times_update = np.zeros(int(self.conf.UPDATE_LOOPS[ep]))
        times_update_target = np.zeros(int(self.conf.UPDATE_LOOPS[ep]))
        for i in range(int(self.conf.UPDATE_LOOPS[ep])):
            # Sample batch of transitions from the buffer
            st = time.time()
            state_batch, partial_reward_to_go_batch, state_next_rollout_batch, d_batch, term_batch, weights_batch, batch_idxes = buffer.sample()
            et = time.time()
            times_sample[i] = et-st
            
            # Update both critic and actor
            st = time.time()
            reward_to_go_batch, critic_value, target_critic_value = self.update(state_batch, state_next_rollout_batch, partial_reward_to_go_batch, d_batch, term_batch, weights_batch)
            et = time.time()
            times_update[i] = et-st

            # Update target critic
            if not self.conf.MC:
                st = time.time()
                self.update_target(self.target_critic.parameters(), self.critic_model.parameters())
                et = time.time()
                times_update_target[i] = et-st

            update_step_counter += 1

        print(f"Sample times - Avg: {np.mean(times_sample)}; Max:{np.max(times_sample)}; Min: {np.min(times_sample)}\n")
        print(f"Update times - Avg: {np.mean(times_update)}; Max:{np.max(times_update)}; Min: {np.min(times_update)}\n")
        print(f"Target Update times - Avg: {np.mean(times_update_target)}; Max:{np.max(times_update_target)}; Min: {np.min(times_update_target)}\n")
        return update_step_counter
    
    def RL_Solve(self, TO_controls, TO_states):
        ''' Solve RL problem '''
        ep_return = 0                                                               # Initialize the return
        rwrd_arr = np.empty(self.NSTEPS_SH+1)                                         # Reward array
        state_next_rollout_arr = np.zeros((self.NSTEPS_SH+1, self.conf.nb_state))     # Next state array
        partial_reward_to_go_arr = np.empty(self.NSTEPS_SH+1)                         # Partial cost-to-go array
        total_reward_to_go_arr = np.empty(self.NSTEPS_SH+1)                           # Total cost-to-go array
        term_arr = np.zeros(self.NSTEPS_SH+1)                                         # Episode-termination flag array
        term_arr[-1] = 1
        done_arr = np.zeros(self.NSTEPS_SH+1)                                         # Episode-MC-termination flag array

        # START RL EPISODE
        self.control_arr = TO_controls # action clipped in TO
        
        for step_counter in range(self.NSTEPS_SH):
            # Simulate actions and retrieve next state and compute reward
            if step_counter == self.NSTEPS_SH-1:
                self.state_arr[step_counter+1,:], rwrd_arr[step_counter] = self.env.step(self.state_arr[step_counter,:], self.control_arr[step_counter-1,:])

            else:
                self.state_arr[step_counter+1,:], rwrd_arr[step_counter] = self.env.step(self.state_arr[step_counter,:], self.control_arr[step_counter,:])

            # Compute end-effector position
            self.ee_pos_arr[step_counter+1,:] = self.env.ee(self.state_arr[step_counter+1, :])
        rwrd_arr[-1] = self.env.reward(self.state_arr[-1,:])

        ep_return = sum(rwrd_arr)

        # Store transition after computing the (partial) cost-to go when using n-step TD (from 0 to Monte Carlo)
        for i in range(self.NSTEPS_SH+1):
            # set final lookahead step depending on whether Monte Cartlo or TD(n) is used
            if self.conf.MC:
                final_lookahead_step = self.NSTEPS_SH
                done_arr[i] = 1 
            else:
                final_lookahead_step = min(i+self.conf.nsteps_TD_N, self.NSTEPS_SH)
                if final_lookahead_step == self.NSTEPS_SH:
                    done_arr[i] = 1 
                else:
                    state_next_rollout_arr[i,:] = self.state_arr[final_lookahead_step+1,:]
            
            # Compute the partial and total cost to go
            partial_reward_to_go_arr[i] = np.float32(sum(rwrd_arr[i:final_lookahead_step+1]))
            total_reward_to_go_arr[i] = np.float32(sum(rwrd_arr[i:self.NSTEPS_SH+1]))

        return self.state_arr, partial_reward_to_go_arr, total_reward_to_go_arr, state_next_rollout_arr, done_arr, rwrd_arr, term_arr, ep_return, self.ee_pos_arr
    
    def RL_save_weights(self, update_step_counter='final'):
        ''' Save NN weights '''
        actor_model_path = f"{self.conf.NNs_path}/N_try_{self.N_try}/actor_{update_step_counter}.pth"
        critic_model_path = f"{self.conf.NNs_path}/N_try_{self.N_try}/critic_{update_step_counter}.pth"
        target_critic_path = f"{self.conf.NNs_path}/N_try_{self.N_try}/target_critic_{update_step_counter}.pth"

        # Save model weights
        torch.save(self.actor_model.state_dict(), actor_model_path)
        torch.save(self.critic_model.state_dict(), critic_model_path)
        torch.save(self.target_critic.state_dict(), target_critic_path)

    def create_TO_init(self, ep, ICS):
        ''' Create initial state and initial controls for TO '''
        self.init_rand_state = ICS    
        
        self.NSTEPS_SH = self.conf.NSTEPS - int(self.init_rand_state[-1]/self.conf.dt)
        if self.NSTEPS_SH == 0:
            return None, None, None, None, 0

        # Initialize array to store RL state, control, and end-effector trajectories
        self.control_arr = np.empty((self.NSTEPS_SH, self.conf.nb_action))
        self.state_arr = np.empty((self.NSTEPS_SH+1, self.conf.nb_state))
        self.ee_pos_arr = np.empty((self.NSTEPS_SH+1,3))

        # Set initial state and end-effector position
        self.state_arr[0,:] = self.init_rand_state
        self.ee_pos_arr[0,:] = self.env.ee(self.state_arr[0, :])

        # Initialize array to initialize TO state and control variables
        init_TO_controls = np.zeros((self.NSTEPS_SH, self.conf.nb_action))
        init_TO_states = np.zeros(( self.NSTEPS_SH+1, self.conf.nb_state))

        # Set initial state 
        init_TO_states[0,:] = self.init_rand_state

        # Simulate actor's actions to compute the state trajectory used to initialize TO state variables (use ICS for state and 0 for control if it is the first episode otherwise use policy rollout)
        success_init_flag = 1
        for i in range(self.NSTEPS_SH):   
            if ep == 0:
                init_TO_controls[i,:] = np.zeros(self.conf.nb_action)
            else:
                init_TO_controls[i,:] = self.NN.eval(self.actor_model, torch.tensor(np.array([init_TO_states[i,:]]), dtype=torch.float32)).squeeze().detach().cpu().numpy()
                print(f"init TO controls {i+1}/{self.NSTEPS_SH}:  {init_TO_controls[i,:]}")
            init_TO_states[i+1,:] = self.env.simulate(init_TO_states[i,:],init_TO_controls[i,:])

            if np.isnan(init_TO_states[i+1,:]).any():
                success_init_flag = 0
                return None, None, None, None, success_init_flag

        return self.init_rand_state, init_TO_states, init_TO_controls, self.NSTEPS_SH, success_init_flag