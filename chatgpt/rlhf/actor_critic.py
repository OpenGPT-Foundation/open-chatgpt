from collections import namedtuple
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops.layers.torch import Rearrange
from transformers import AutoModelForCausalLM, AutoTokenizer
from transformers.modeling_outputs import ModelOutput

from chatgpt.models.generation import generate

ActorCriticReturn = namedtuple('ActionCriticReturn', [
    'actions',
    'action_logits',
    'values',
    'sequences_actor',
    'sequences_mask_actor',
    'sequences_critic',
    'sequences_mask_critic',
    'action_len_actor',
    'action_len_critic',
])


@dataclass
class CausalLMOutputWithValue(ModelOutput):
    loss: Optional[torch.FloatTensor] = None
    logits: Optional[torch.FloatTensor] = None
    past_key_values: Optional[Tuple[Tuple[torch.FloatTensor]]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None
    cross_attentions: Optional[Tuple[torch.FloatTensor]] = None
    value: Optional[torch.FloatTensor] = None


class ActorModel(nn.Module):
    """Actor model that generates logits representing the probability
    distribution over the vocabulary of actions.

    Args:
        pretrained (str, optional): Pretrained model name or path.
        debug (bool, optional): Whether to print debug information. Defaults to False.
    """
    def __init__(self, pretrained: Optional[str] = None, debug: bool = False):
        super().__init__()

        # Load tokenizer and set special tokens
        self.tokenizer = AutoTokenizer.from_pretrained(
            pretrained,
            padding_side='left',
            padding=True,
            truncation=True,
        )
        # add eos token if not present
        if self.tokenizer.eos_token is None:
            self.tokenizer.eos_token = '</s>'
            self.tokenizer.eos_token_id = 0
        self.tokenizer.pad_token = self.tokenizer.eos_token
        self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

        # Load pre-trained language model
        self.model = AutoModelForCausalLM.from_pretrained(pretrained)

        self.debug = debug

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        position_ids: Optional[List[torch.FloatTensor]] = None,
        head_mask: Optional[torch.Tensor] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = True,
        return_dict: Optional[bool] = True,
    ) -> Union[Tuple, ModelOutput]:
        """Generate logits to have probability distribution over the vocabulary
        of the actions.

        Args:
            input_ids (torch.Tensor): Sequences of states and actions used to compute token logits
            for the whole list of sequences.
            attention_mask (torch.Tensor): Mask for the sequences attention.

        Returns:
            logits (torch.Tensor): Logits for the actions taken.
        """
        model_output = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        logits = model_output['logits']
        if self.debug:
            print('ActorModel.forward')
            print('logits shape:', model_output.logits.shape)
            print('logits:', model_output.logits)
        return logits

    @torch.no_grad()
    def generate_(
        self,
        input_ids: torch.Tensor,
        return_action_mask: bool = True,
        **kwargs
    ) -> Union[Tuple[torch.LongTensor, torch.LongTensor], Tuple[
            torch.LongTensor, torch.LongTensor, torch.BoolTensor]]:
        sequences = generate(self.model, input_ids, **kwargs)
        print(sequences)
        attention_mask = None
        pad_token_id = kwargs.get('pad_token_id', None)
        if pad_token_id is not None:
            attention_mask = sequences.not_equal(pad_token_id).to(
                dtype=torch.long, device=sequences.device)
        if not return_action_mask:
            return sequences, attention_mask, None
        input_len = input_ids.size(1)
        eos_token_id = kwargs.get('eos_token_id', None)
        if eos_token_id is None:
            action_mask = torch.ones_like(sequences, dtype=torch.bool)
        else:
            # left padding may be applied, only mask action
            action_mask = (sequences[:, input_len:] == eos_token_id).cumsum(
                dim=-1) == 0
            action_mask = F.pad(action_mask, (1 + input_len, -1),
                                value=True)  # include eos token and input
        action_mask[:, :input_len] = False
        action_mask = action_mask[:, 1:]
        return sequences, attention_mask, action_mask[:, -(sequences.size(1) -
                                                           input_len):]

    @torch.no_grad()
    def generate(self, input_ids: torch.Tensor, attention_mask: torch.Tensor,
                 temperature: float, max_sequence_length: int, max_tokens: int,
                 min_tokens: int,
                 **kwargs) -> Tuple[torch.Tensor, torch.Tensor]:
        """Generate actions and sequences=[states, actions] from state (i.e.
        input of the prompt generator model)

        Args:
            states (torch.Tensor): Input sequence tensor with only state IDs.
            state_mask (torch.Tensor): Attention mask for input state tensor.
            temperature (float): Softmax temperature to apply during generation.
            max_sequence_length (int): Maximum allowed length of generated sequence.
            max_tokens (int): Maximum number of tokens to generate after input sequence.

        Returns:
            Tuple[torch.Tensor, torch.Tensor]: Tuple of generated actions and full generated sequences.
        """
        # Set maximum length of generation
        max_generation_possible = max_sequence_length - input_ids.shape[1]
        max_completion = min(max_tokens, max_generation_possible)
        if max_generation_possible < min_tokens:
            raise ValueError(
                f'The prompt is too long w.r.t the '
                f'model sequence length \n'
                f'max_sequence_length={max_sequence_length}\n'
                f'state_length={input_ids.shape[1]}\n'
                f'min_tokens={min_tokens}\n'
                f'max_tokens={max_tokens}\n'
                f'max_generation_possible={max_generation_possible}\n')

        # Generate actions and sequences
        sequences = self.model.generate(
            input_ids=input_ids,
            attention_mask=attention_mask,
            temperature=temperature,
            max_new_tokens=max_completion,
            no_repeat_ngram_size=3,
        )
        actions = sequences[:, input_ids.shape[1]:]
        # Extract generated actions from full sequence
        if self.debug:
            print('ActorModel.generate')
            print('state', input_ids)
            print('state shape', input_ids.shape)
            print('sequence shape', sequences.shape)
            print('sequence', sequences)
            print('actions shape', actions.shape)
            print('actions', actions)
        return actions, sequences


class CriticModel(nn.Module):
    """Critic model that evaluates the quality of a given sequence of tokens.

    Args:
        pretrained (str): Pretrained model name or path.
        debug (bool): Whether to print debugging information or not.
    """
    def __init__(self,
                 model='opt',
                 pretrained: Optional[str] = None,
                 debug: bool = True):
        super().__init__()

        # Instantiate tokenizer and model from pretrained checkpoint
        self.tokenizer = AutoTokenizer.from_pretrained(
            pretrained,
            padding_side='left',
            truncation=True,
            padding=True,
        )
        self.model = AutoModelForCausalLM.from_pretrained(pretrained)

        # Set EOS token and padding token
        if self.tokenizer.eos_token is None:
            self.tokenizer.eos_token = '</s>'
            self.tokenizer.eos_token_id = 2
            # add pad token if not present
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

        self.debug = debug
        self.config = self.model.config

        # Define value head layers to output a scalar value
        if model == 'opt':
            head_hidden_size = self.config.word_embed_proj_dim
        elif model == 'gpt2':
            head_hidden_size = self.config.n_embd
        else:
            head_hidden_size = self.config.head_hidden_size
        self.value_head = nn.Sequential(
            nn.Linear(head_hidden_size, head_hidden_size),
            nn.ReLU(),
            nn.Linear(head_hidden_size, 1),
            Rearrange('... 1 -> ...'),
        )

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        position_ids: Optional[List[torch.FloatTensor]] = None,
        head_mask: Optional[torch.Tensor] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = True,
    ) -> Union[Tuple, CausalLMOutputWithValue]:
        """Evaluate the quality of a sequence of tokens.

        Args:
            input_ids (torch.Tensor): Tensor of token ids of shape (batch_size, seq_length)
            attention_mask (torch.Tensor): Mask tensor of shape (batch_size, seq_length)

        Returns:
            torch.Tensor: Tensor of rewards of shape (batch_size, 1)
        """
        output = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        value = self.value_head(output.last_hidden_state)
        # Print debugging information
        if self.debug:
            print('CriticModel.forward')
            print('input_ids.shape', input_ids.shape)
            print('input_ids', input_ids)
            print('rewards.shape', value.shape)
            print('rewards', value)

        return value

    def get_reward(self, output_sequence: torch.Tensor,
                   output_sequence_mask: torch.Tensor) -> torch.Tensor:
        """Get the reward for a sequence of tokens.

        Args:
            input_ids (torch.Tensor): Tensor of token ids of shape (batch_size, seq_length)
            attention_mask (torch.Tensor): Mask tensor of shape (batch_size, seq_length)

        Returns:
            torch.Tensor: Tensor of rewards of shape (batch_size,)
        """
        if output_sequence.shape[1] > self.config.max_sequence_length:
            raise ValueError(
                f'Output sequence is too long: {output_sequence.shape[1]}'
                f' > {self.config.max_sequence_length}')
        value = self.forward(output_sequence, output_sequence_mask)
        return value[:, -1]


class ActorCritic(nn.Module):
    """Actor Critic class stores both the actor and the critic models and it
    generates values and action for given sequences during the training of the
    actor.

    Args:
        actor (nn.Module): Actor model
        critic (nn.Module): Critic model
        debug (bool): enable prints for Debugging

    Methods:
        forward: given a sequence returns action logits and values (used
            to evaluate the actor during training)
        generate: given a sequence returns action, action logits, values
            sequences and sequences masks (used to generate new sequences
            during acting phase)
    """
    def __init__(
        self,
        actor: ActorModel,
        critic: CriticModel,
        debug: bool = False,
    ):
        super().__init__()
        self.actor = actor
        self.critic = critic
        self.debug = debug

    def forward(
        self,
        sequences_actor: torch.Tensor,
        sequences_mask_actor: torch.Tensor,
        sequences_critic: torch.Tensor,
        sequences_mask_critic: torch.Tensor,
        action_len_actor: int,
        action_len_critic: int,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Given the whole sequences, use the actor forward to get the logits
        for each token in the sequence and the critic forward to get the values
        for each generation step.

        Args:
            sequences (torch.Tensor): Sequences composed of [states, actions]
            sequences_mask (torch.Tensor): Mask for the sequences
            action_len (int): Length of the actions in the sequences

        Returns:
            action_logits (torch.Tensor): Logits for the actions in the
                sequences
            values (torch.Tensor): Values for the actions in the sequences
        """
        # use a single forward on the whole sequence
        # to get pi(y | x) and ignore predicted output
        actions_logits = self.actor(sequences_actor, sequences_mask_actor)
        values = self.critic(sequences_critic, sequences_mask_critic)

        # return only logits and values for the actions taken
        real_actions_logits = actions_logits[:, -action_len_actor:, :]
        real_values = values[:, -action_len_critic:]

        if self.debug:
            print('ActorCritic.forward')
            print('action_len_actor', action_len_actor)
            print('action_len_critic', action_len_critic)
            print('sequences_actor.shape', sequences_actor.shape)
            print('sequences_actor', sequences_actor)
            print('sequences_mask_actor.shape', sequences_mask_actor.shape)
            print('sequences_mask_actor', sequences_mask_actor)
            print('sequences_critic.shape', sequences_critic.shape)
            print('sequences_critic', sequences_critic)
            print('sequences_mask_critic.shape', sequences_mask_critic.shape)
            print('sequences_mask_critic', sequences_mask_critic)

            print('real_action_logits.shape', real_actions_logits.shape)
            print('real_action_logits', real_actions_logits)
            print('real_values.shape', real_values.shape)
            print('real_values', real_values)

        return real_actions_logits, real_values

    @torch.no_grad()
    def generate(
        self,
        states_actor: torch.Tensor,
        state_mask_actor: torch.Tensor,
        states_critic,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Generate actions, actions_logits, values and sequences from states.

        Args:
            states_actor (torch.Tensor): States for the actor
            states_mask_actor (torch.Tensor): Mask for the states for the
                actor
            states_critic (torch.Tensor): States for the critic

        Returns:
            actions (torch.Tensor): Actions generated from the states
            actions_logits (torch.Tensor): Logits for the actions generated
                from the states (i.e. pi(y | x))
            values (torch.Tensor): Values generated by the critic model
                for the actions generated by the actor (i.e. V(x))
            sequences (torch.Tensor): Sequences generated from the states
                as [states, actions]
        """
        # Generate action sequence from actor.
        actions, sequences_actor = self.actor.generate(states_actor,
                                                       state_mask_actor)

        # Get the mask for the generated sequence.
        sequences_mask_actor = sequences_actor != self.actor.tokenizer.pad_token_id
        sequences_mask_actor = sequences_mask_actor.to(
            sequences_actor.device).long().detach()

        # Get the length of the generated actions.
        action_len_actor = actions.shape[1]

        sequences_critic = sequences_actor
        sequences_mask_critic = sequences_mask_actor
        action_len_critic = action_len_actor

        # Generate action logits and values.
        actions_logits, values = self.forward(
            sequences_actor,
            sequences_mask_actor,
            sequences_critic,
            sequences_mask_critic,
            action_len_actor,
            action_len_critic,
        )

        if self.debug:
            print('ActorCritic.generate')
            print('actions shape', actions.shape)
            print('actions', actions)
            print('sequence shape', sequences_actor.shape)
            print('sequence', sequences_actor)
            print('actions_logits shape', actions_logits.shape)
            print('actions_logits', actions_logits)
            print('values shape', values.shape)
            print('values', values)

        return ActorCriticReturn(
            actions,
            actions_logits,
            values,
            sequences_actor,
            sequences_mask_actor,
            sequences_critic,
            sequences_mask_critic,
            action_len_actor,
            action_len_critic,
        )
