import numpy as np
import tensorflow as tf

from tensorflow.contrib.rnn.python.ops import core_rnn_cell_impl
from tensorflow.contrib import seq2seq
from tensorflow.contrib.seq2seq.python.ops import decoder_fn as decoder_fn_lib
from tensorflow.contrib import layers


def birnn_with_projection(size, fused_rnn_constructor, inputs, length,
                          share_rnn=False, projection_scope=None):
    projection_initializer = tf.constant_initializer(
        np.concatenate([np.eye(size), np.eye(size)]))
    fused_rnn = fused_rnn_constructor(size)
    with tf.variable_scope("RNN", reuse=share_rnn):
        encoded = fused_birnn(fused_rnn, inputs, sequence_length=length,
                              dtype=tf.float32, time_major=False)[0]
        encoded = tf.concat(encoded, 2)

    projected = tf.contrib.layers.fully_connected(
        encoded, size, activation_fn=None,
        weights_initializer=projection_initializer,
        scope=projection_scope)
    return projected


def fused_rnn_backward(fused_rnn, inputs, sequence_length,
                       initial_state=None, dtype=None,
                       scope=None, time_major=True):
    if not time_major:
        inputs = tf.transpose(inputs, [1, 0, 2])
    # assumes that time dim is 0 and batch is 1
    rev_inputs = tf.reverse_sequence(inputs, sequence_length, 0, 1)
    rev_outputs, last_state = fused_rnn(
        rev_inputs, sequence_length=sequence_length,
        initial_state=initial_state, dtype=dtype, scope=scope)
    outputs = tf.reverse_sequence(rev_outputs, sequence_length, 0, 1)
    if not time_major:
        outputs = tf.transpose(outputs, [1, 0, 2])
    return outputs, last_state


def fused_birnn(fused_rnn, inputs, sequence_length, initial_state=None, dtype=None, scope=None, time_major=True,
                backward_device=None):
    with tf.variable_scope(scope or "BiRNN"):
        sequence_length = tf.cast(sequence_length, tf.int32)
        if not time_major:
            inputs = tf.transpose(inputs, [1, 0, 2])
        outputs_fw, state_fw = fused_rnn(inputs, sequence_length=sequence_length, initial_state=initial_state,
                                         dtype=dtype, scope="FW")

        if backward_device is not None:
            with tf.device(backward_device):
                outputs_bw, state_bw = fused_rnn_backward(fused_rnn, inputs, sequence_length, initial_state, dtype,
                                                          scope="BW")
        else:
            outputs_bw, state_bw = fused_rnn_backward(fused_rnn, inputs, sequence_length, initial_state, dtype,
                                                      scope="BW")

        if not time_major:
            outputs_fw = tf.transpose(outputs_fw, [1, 0, 2])
            outputs_bw = tf.transpose(outputs_bw, [1, 0, 2])
    return (outputs_fw, outputs_bw), (state_fw, state_bw)




def pair_of_bidirectional_LSTMs(seq1, seq1_lengths, seq2, seq2_lengths,
                                output_size, scope=None, drop_keep_prob=1.0,
                                conditional_encoding=True):
    """Duo of bi-LSTMs over seq1 and seq2 with (optional)conditional encoding.

    Args:
        seq1 (tensor = time x batch x input): The inputs into the first biLSTM
        seq1_lengths (tensor = batch): The lengths of the sequences.
        seq2 (tensor = time x batch x input): The inputs into the second biLSTM
        seq1_lengths (tensor = batch): The lengths of the sequences.
        output_size (int): Size of the LSTMs state.
        scope (string): The TensorFlow scope for the reader.
        drop_keep_drop (float=1.0): The keep propability for dropout.

    Returns:
        Outputs (tensor): The outputs from the second bi-LSTM.
        States (tensor): The cell states from the second bi-LSTM.
    """
    with tf.variable_scope(scope or "paired_LSTM_seq1") as varscope1:
        # seq1_states: (c_fw, h_fw), (c_bw, h_bw)
        _, seq1_final_states = dynamic_bidirectional_lstm(
                        seq1, seq1_lengths, output_size, scope=varscope1,
                        drop_keep_prob=drop_keep_prob)

    with tf.variable_scope(scope or "paired_LSTM_seq2") as varscope2:
        varscope1.reuse_variables()
        # each [batch_size x max_seq_length x output_size]
        if conditional_encoding:
            seq2_init_states = seq1_final_states
        else:
            seq2_init_states = None

        all_states_fw_bw, final_states_fw_bw = dynamic_bidirectional_lstm(
                                            seq2, seq2_lengths, output_size,
                                            seq2_init_states, scope=varscope2,
                                            drop_keep_prob=drop_keep_prob)

    return all_states_fw_bw, final_states_fw_bw


def dynamic_bidirectional_lstm(inputs, lengths, output_size,
                               initial_state=(None, None), scope=None,
                               drop_keep_prob=1.0):
    """Dynamic bi-LSTM reader, with optional initial state.

    Args:
        inputs (tensor): The inputs into the bi-LSTM
        lengths (tensor): The lengths of the sequences
        output_size (int): Size of the LSTM state of the reader.
        initial_state (tensor=None, tensor=None): Tuple of initial
                                            (forward, backward) states
                                            for the LSTM
        scope (string): The TensorFlow scope for the reader.
        drop_keep_drop (float=1.0): The keep probability for dropout.

    Returns:
        all_states (tensor): All forward and backward states
        final_states (tensor): The final forward and backward states
    """
    with tf.variable_scope(scope or "reader") as varscope:
        varscope
        cell_fw = tf.contrib.rnn.LSTMCell(
            output_size,
            state_is_tuple=True,
            initializer=tf.contrib.layers.xavier_initializer()
        )
        cell_bw = tf.contrib.rnn.LSTMCell(
            output_size,
            state_is_tuple=True,
            initializer=tf.contrib.layers.xavier_initializer()
        )

        if drop_keep_prob != 1.0:
            cell_fw = tf.contrib.rnn.DropoutWrapper(
                                    cell=cell_fw,
                                    output_keep_prob=drop_keep_prob,
                                    input_keep_prob=drop_keep_prob, seed=1233)
            cell_bw = tf.contrib.rnn.DropoutWrapper(
                cell=cell_bw,
                output_keep_prob=drop_keep_prob,
                input_keep_prob=drop_keep_prob, seed=1233)

        all_states_fw_bw, final_states_fw_bw = tf.nn.bidirectional_dynamic_rnn(
            cell_fw,
            cell_bw,
            inputs,
            sequence_length=lengths,
            initial_state_fw=initial_state[0],
            initial_state_bw=initial_state[1],
            dtype=tf.float32
        )

        return all_states_fw_bw, final_states_fw_bw


def dynamic_lstm_decoder(targets, target_lengths, output_size,
                         input_state, decoder_vocab, max_decoder_seq_len,
                         start_of_sequence_id, end_of_sequence_id,
                         scope=None, drop_keep_prob=1.0):
    """Dynamic RNN decoder using LSTM, from initial input state.

    Args:
        targets (tensor):  The decoder targets, of size
                           [batch_size x max_target_length x output_size]
        target_lengths (tensor):  The lengths of the targets sentences, of size
                                  [batch_size]
        output_size (int):  Size of the LSTM state of the reader.
        decoder_vocab:  Neural vocab of targets
        max_decoder_seq_len:  Maximum target length
        start_of_sequence_id: Set to decoder_vocab.get_id(decoder_symbol_start)
        end_of_sequence_id:  Set to decoder_vocab.get_id(decoder_symbol_end)
        input_state (tensor):  The input state that the decoder starts from.
        scope (string):  The TensorFlow scope for the decoder.
        drop_keep_drop (float=1.0):  The keep probability for dropout.

    Returns:
        decoder_outputs_train:  Outputs of the decoder at train time, of size
            [batch_size x max_time]
            where max_time = max sequence length in batch
        decoder_logits_train:  Logits of the training output, of size
            [batch_size x decoder_sequence_length x num_decoder_symbols]
        decoder_outputs_infer:  Output at inference time
            [batch_size x decoder_sequence_length]  (?)
    """
    # First get parameters
    num_decoder_symbols = len(decoder_vocab)
    decoder_embeddings = decoder_vocab.get_embedding_matrix()
    decoder_sequence_length = max_decoder_seq_len

    # Construct decoder
    with tf.variable_scope(scope or "decoder") as varscope:
        varscope
        # Sanity check: we expect input to be an LSTM output state
        assert isinstance(input_state, tf.contrib.rnn.LSTMStateTuple)
        input_state_dim = input_state.c.get_shape()[1]
        assert input_state_dim == input_state.h.get_shape()[1]
        decoder_input = input_state
        # Ensure input state is of specified dimensionality
        # E.g., for bidir LSTM, the final_reader_state will be concatenation
        # of forward and backward pass states, and hence 2x too big
        if (input_state_dim != output_size):
            init_h = tf.contrib.layers.xavier_initializer(
                uniform=True)  # uniform=False for truncated normal
            decoder_init_state_h = tf.contrib.layers.fully_connected(
                input_state.h, output_size,
                weights_initializer=init_h,
                activation_fn=None
            )
            init_c = tf.contrib.layers.xavier_initializer(
                uniform=True)  # uniform=False for truncated normal
            decoder_init_state_c = tf.contrib.layers.fully_connected(
                input_state.c, output_size,
                weights_initializer=init_c,
                activation_fn=None
            )
            decoder_input = core_rnn_cell_impl.LSTMStateTuple(
                decoder_init_state_c, decoder_init_state_h)

        decoder_cell = tf.contrib.rnn.LSTMCell(
            output_size,
            state_is_tuple=True,
            initializer=tf.contrib.layers.xavier_initializer()
        )
        if (drop_keep_prob != 1.0):
            decoder_cell = tf.contrib.rnn.DropoutWrapper(
                decoder_cell,
                input_keep_prob=drop_keep_prob,
                output_keep_prob=drop_keep_prob
            )

        # Decoder model for training
        decoder_fn_train = decoder_fn_lib.simple_decoder_fn_train(
            encoder_state=decoder_input)
        decoder_outputs_train, decoder_state_train, _ = seq2seq.dynamic_rnn_decoder(
            cell=decoder_cell,
            decoder_fn=decoder_fn_train,
            inputs=targets,
            sequence_length=target_lengths,
            time_major=False  # batch major!
            # scope=scope
        )

        def output_fn(x):
            layers.linear(x, num_decoder_symbols, scope=scope)
        decoder_logits_train = tf.map_fn(output_fn,
                                         decoder_outputs_train)

        # Decoder model for testing [not validated yet ;-)]
        decoder_fn_infer = decoder_fn_lib.simple_decoder_fn_inference(
            output_fn=output_fn,
            encoder_state=decoder_input,
            embeddings=decoder_embeddings,
            start_of_sequence_id=start_of_sequence_id,
            end_of_sequence_id=end_of_sequence_id,
            # TODO: find out why it goes to +1  [comment from seq2seq_test.py]
            # I.e., no idea why the -1, but we have to, as in seq2seq_test ;-)
            maximum_length=decoder_sequence_length - 1,
            num_decoder_symbols=num_decoder_symbols,
            dtype=dtypes.int32)
        decoder_outputs_infer, decoder_state_infer, _ = seq2seq.dynamic_rnn_decoder(
            cell=decoder_cell,
            decoder_fn=decoder_fn_infer,
            time_major=True,
            scope=scope
        )

        return (decoder_outputs_train,
                decoder_logits_train,
                decoder_outputs_infer)


def dynamic_lstm_decoder_loss(decoder_logits_train,
                              targets,
                              target_lengths,
                              num_decoder_symbols,
                              scope=None):
    """Loss for dynamic RNN decoder as constructed by dynamic_lstm_decoder.

    Args:
        decoder_logits_train (tensor):  The decoder output logits, should be of
            [batch_size x max_time x num_decoder_symbols]
            where max_time = max sequence length in batch
        targets (tensor):  The decoder targets, of size
                           [batch_size x max_target_length x output_size]
        target_lengths (tensor):  The lengths of the targets sentences, of size
                                  [batch_size]
        num_decoder_symbols:  Size of the output symbol vocabulary, e.g.,
            len(decoder_vocab)
        scope (string):  The TensorFlow scope for the loss

    Returns:
        decoder_loss:  Loss for the decoder, based on 
            tf.losses.sparse_softmax_cross_entropy
        decoder_probs:  Simply the sigmoid of the input logits
    """

    with tf.variable_scope(scope or "decoder-loss") as varscope:
        varscope
        logits_flat = tf.reshape(
            decoder_logits_train, [-1, num_decoder_symbols])

        # Get max_interpretation_length over batch
        # this is max over values in placeholders['interpretation_lengths']
        # it is NOT the shape of targets
        max_interpr_seq_len_batch = tf.reduce_max(
            target_lengths)
        # Truncate label sequences to max_interpr_seq_len_batch
        labels_trunc = tf.slice(
            targets,
            [0, 0],  # begin = 1st tensor element
            [-1, max_interpr_seq_len_batch]  # restrict 2nd dim only
        )
        labels_flat = tf.reshape(labels_trunc, [-1])

        # Now get weight matrix
        ones = tf.ones_like(target_lengths, dtype=tf.float32)
        label_weights = tf.where(
            tf.not_equal(target_lengths, 0),
            ones,
            tf.zeros_like(target_lengths, dtype=tf.float32)
        )
        label_divisors = tf.where(
            tf.not_equal(label_weights, tf.constant(0, dtype=tf.float32)),
            tf.cast(target_lengths, tf.float32),
            ones)
        # Thanks https://stackoverflow.com/questions/35361467/tensorflow-numpy-repeat-alternative
        label_weights = tf.div(label_weights, label_divisors)
        label_weights = tf.tile(label_weights, [max_interpr_seq_len_batch])
        label_weights = tf.transpose(tf.reshape(label_weights, [-1, 2]))
        label_weights_flat = tf.reshape(label_weights, [-1])
        # Finally, calculate cross entropy loss using the weights
        crossent = tf.losses.sparse_softmax_cross_entropy(
            labels=labels_flat,
            logits=logits_flat,
            weights=label_weights_flat)
        crossent = tf.reduce_mean(crossent)
        interpretation_loss = crossent
        decoder_probs = tf.nn.sigmoid(decoder_logits_train)

        return interpretation_loss, decoder_probs