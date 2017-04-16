from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os, sys
if sys.version_info > (3, 0):
    from six.moves import xrange
import re

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import numpy as np
import tensorflow as tf

from encoder_decoder import classifiers, data_utils, graph_utils
from bashlex import data_tools
from nlp_tools import constants, slot_filling, tokenizer
from eval.eval_archive import DBConnection


def demo(sess, model, FLAGS):
    """
    Simple command line decoding interface.
    """

    slot_filling_classifier = None
    if FLAGS.fill_argument_slots:
        # create slot filling classifier
        mapping_param_dir = os.path.join(FLAGS.model_dir,
            'train.{}.mappings.X.Y.npz'.format(FLAGS.sc_vocab_size))
        train_X, train_Y = data_utils.load_slot_filling_data(mapping_param_dir)
        slot_filling_classifier = \
                classifiers.KNearestNeighborModel(FLAGS.num_nn_slot_filling,
                                                  train_X, train_Y)
        print('Slot filling classifier parameters loaded.')

    # Decode from standard input.
    sys.stdout.write("> ")
    sys.stdout.flush()
    sentence = sys.stdin.readline()

    vocabs = data_utils.load_vocab(FLAGS)

    while sentence:
        batch_outputs, output_logits = translate_fun(sentence, sess, model,
            vocabs, FLAGS, slot_filling_classifier=slot_filling_classifier)

        if FLAGS.token_decoding_algorithm == "greedy":
            tree, pred_cmd, outputs = batch_outputs[0]
            score = output_logits[0]
            print("{} ({})".format(pred_cmd, score))
        elif FLAGS.token_decoding_algorithm == "beam_search":
            if batch_outputs:
                top_k_predictions = batch_outputs[0]
                top_k_scores = output_logits[0]
                for j in xrange(min(FLAGS.beam_size, 10, len(batch_outputs[0]))):
                    if len(top_k_predictions) <= j:
                        break
                    top_k_pred_tree, top_k_pred_cmd, top_k_outputs = \
                        top_k_predictions[j]
                    print("Prediction {}: {} ({}) ".format(
                        j+1, top_k_pred_cmd, top_k_scores[j]))
                print()
            else:
                print("I'm very sorry, I can't translate this command at the moment.")
        print("> ", end="")
        sys.stdout.flush()
        sentence = sys.stdin.readline()


def translate_fun(sentence, sess, model, vocabs, FLAGS,
                  slot_filling_classifier=None):
    # Get token-ids for the input sentence.
    # entities: ner_by_token_id, ner_by_char_pos, ner_by_category
    sc_vocab, _, _, rev_tg_vocab = vocabs[:4]
    rev_tg_char_vocab = vocabs[-1] if FLAGS.tg_char else None

    if FLAGS.explain:
        tokens = data_tools.bash_tokenizer(sentence, arg_type_only=FLAGS.normalized)
        token_ids, _ = data_utils.sentence_to_token_ids(tokens, sc_vocab,
                data_tools.bash_tokenizer, None)
    else:
        if FLAGS.char:
            token_ids, entities = data_utils.sentence_to_token_ids(sentence,
                sc_vocab, data_tools.char_tokenizer, tokenizer.basic_tokenizer)
            token_full_ids = []
        else:
            token_ids, entities = data_utils.sentence_to_token_ids(
                sentence, sc_vocab, tokenizer.ner_tokenizer, None)
            token_full_ids, _ = data_utils.sentence_to_token_ids(
                sentence, sc_vocab, tokenizer.basic_tokenizer, None,
                use_unk=False)
    
    # Which bucket does it belong to?
    bucket_id = min([b for b in xrange(len(model.buckets))
                    if model.buckets[b][0] > len(token_ids)])

    # Get a 1-element batch to feed the sentence to the model.
    formatted_example = model.format_example(
        [[token_ids], [token_full_ids]], [[[data_utils.ROOT_ID]], [[data_utils.ROOT_ID]]],
        bucket_id=bucket_id)

    # Decode the ouptut for this 1-element batch.
    # Non-grammatical templates and templates that cannot hold all fillers are
    # filtered out.
    # TODO: align output commands and their scores correctly
    model_outputs = model.step(sess, formatted_example, bucket_id,
        forward_only=True, return_rnn_hidden_states=FLAGS.fill_argument_slots)
    output_symbols = model_outputs.output_symbols
    output_logits = model_outputs.output_logits
    losses = model_outputs.losses
    attn_alignments = model_outputs.attn_alignments

    char_output_symbols = model_outputs.char_output_symbols if FLAGS.tg_char \
        else None
    nl_fillers, encoder_outputs, decoder_outputs = None, None, None
    if FLAGS.fill_argument_slots:
        assert(slot_filling_classifier is not None)
        nl_fillers = entities[0]
        encoder_outputs = model_outputs.encoder_hidden_states
        decoder_outputs = model_outputs.decoder_hidden_states
    batch_outputs = decode(output_symbols, rev_tg_vocab, FLAGS,
                           char_output_symbols=char_output_symbols,
                           rev_tg_char_vocab=rev_tg_char_vocab,
                           grammatical_only=FLAGS.grammatical_only,
                           nl_fillers=nl_fillers,
                           slot_filling_classifier=slot_filling_classifier,
                           encoder_outputs=encoder_outputs,
                           decoder_outputs=decoder_outputs)

    return batch_outputs, output_logits


def decode(output_symbols, rev_tg_vocab, FLAGS, char_output_symbols=None,
           rev_tg_char_vocab=None, grammatical_only=True, nl_fillers=None,
           slot_filling_classifier=None, encoder_outputs=None,
           decoder_outputs=None):
    """
    Transform the neural network output into readable strings and apply the
    relevant filters.
    """
    if nl_fillers is None:
        assert(slot_filling_classifier is None)
        assert(encoder_outputs is None)
        assert(decoder_outputs is None)

    # def to_readable(outputs, rev_tg_vocab):
    #     search_history = [data_utils._ROOT]
    #     for output in outputs:
    #         if output < len(rev_tg_vocab):
    #             search_history.append(rev_tg_vocab[output])
    #         else:
    #             search_history.append(data_utils._UNK)
    #     tree = data_tools.list2ast(search_history)
    #     cmd = data_tools.ast2command(tree, loose_constraints=True)
    #     return tree, cmd, search_history

    batch_outputs = []
    num_output_examples = 0

    for i in xrange(len(output_symbols)):
        top_k_predictions = output_symbols[i]
        assert((FLAGS.token_decoding_algorithm == "greedy") or 
               len(top_k_predictions) == FLAGS.beam_size)
        if FLAGS.token_decoding_algorithm == "beam_search":
            beam_outputs = []
        else:
            top_k_predictions = [top_k_predictions]
        for j in xrange(len(top_k_predictions)):

            # Step 1: transform the neural network output into readable strings
            prediction = top_k_predictions[j]
            outputs = [int(pred) for pred in prediction]
            # If there is an EOS symbol in outputs, cut them at that point.
            if data_utils.EOS_ID in outputs:
                outputs = outputs[:outputs.index(data_utils.EOS_ID)]

            if nl_fillers is not None:
                cm_slots = {}

            tree, output_tokens = None, []
            if FLAGS.char:
                tg = "".join([tf.compat.as_str(rev_tg_vocab[output])
                    for output in outputs]).replace(data_utils._UNK, ' ')
            else:
                for ii in xrange(len(outputs)):
                    output = outputs[ii]
                    if output < len(rev_tg_vocab):
                        pred_token = rev_tg_vocab[output]
                        if "@@" in pred_token:
                            pred_token = pred_token.split("@@")[-1]
                        output_tokens.append(pred_token)
                        if nl_fillers is not None and \
                                pred_token in constants._ENTITIES:
                            if ii > 0 and slot_filling.is_min_flag(
                                    rev_tg_vocab[outputs[ii-1]]):
                                pred_token_type = 'Timespan'
                            else:
                                pred_token_type = pred_token
                            cm_slots[ii] = (pred_token, pred_token_type)
                    else:
                        output_tokens.append(data_utils._UNK)
                tg = " ".join(output_tokens)
            
            # check if the predicted command templates have enough slots to
            # hold the fillers (to rule out templates that are trivially
            # unqualified)
            if nl_fillers is None or len(cm_slots) >= len(nl_fillers):
                # Step 2: check if the predicted command template is grammatical
                if not FLAGS.explain:
                    if FLAGS.dataset.startswith("bash"):
                        tg = re.sub('( ;\s+)|( ;$)', ' \\; ', tg)
                        tree = data_tools.bash_parser(tg)
                    else:
                        tree = data_tools.paren_parser(tg)

                # filter out non-grammatical output
                if tree is not None or not grammatical_only:
                    output_example = False
                    if FLAGS.explain or not FLAGS.dataset.startswith("bash"):
                        temp = tg
                        output_example = True
                    else:
                        temp = data_tools.ast2template(tree,
                            loose_constraints=True, ignore_flag_order=False)
                        if nl_fillers is None:
                            output_example = True
                        else:
                            # Step 3: match the fillers to the argument slots
                            tree2, temp, _ = slot_filling.stable_slot_filling(
                                output_tokens, nl_fillers, cm_slots,
                                encoder_outputs[i],
                                decoder_outputs[i*FLAGS.beam_size+j],
                                slot_filling_classifier,
                                verbose=False
                            )
                            if temp is not None:
                                output_example = True
                                tree = tree2
                    if output_example:
                        if FLAGS.token_decoding_algorithm == "greedy":
                            batch_outputs.append((tree, temp, outputs))
                        else:
                            beam_outputs.append((tree, temp, outputs))
                        num_output_examples += 1

            # TODO: the threshold 20 is used since the slot-filling step
            # can be slow
            if num_output_examples == 20:
                break

        if FLAGS.token_decoding_algorithm == "beam_search":
            if beam_outputs:
                batch_outputs.append(beam_outputs)

    if char_output_symbols is not None:
        char_output_symbols = char_output_symbols[0]
        sentence_length = char_output_symbols.shape[0]
        batch_char_outputs = []
        batch_char_predictions = [np.transpose(np.reshape(x, [sentence_length, FLAGS.beam_size, 
                                                 FLAGS.max_tg_token_size + 1]), (1, 0, 2))
                                  for x in np.split(char_output_symbols, FLAGS.batch_size, 1)]
        for batch_id in xrange(len(batch_char_predictions)):
            beam_char_outputs = []
            top_k_char_predictions = batch_char_predictions[batch_id]
            for k in xrange(len(top_k_char_predictions)):
                top_k_char_prediction = top_k_char_predictions[k]
                sent = []
                for i in xrange(sentence_length):
                    word = ''
                    for j in xrange(FLAGS.max_tg_token_size):
                        char_prediction = top_k_char_prediction[i, j]
                        if char_prediction == data_utils.CEOS_ID or \
                            char_prediction == data_utils.CPAD_ID:
                            break
                        elif char_prediction in rev_tg_char_vocab:
                            word += rev_tg_char_vocab[char_prediction]
                        else:
                            word += data_utils._CUNK
                    sent.append(word)
                if data_utils._CATOM in sent:
                    sent = sent[:sent[:].index(data_utils._CATOM)]
                beam_char_outputs.append(' '.join(sent))
            batch_char_outputs.append(beam_char_outputs)
        return batch_outputs, batch_char_outputs
    else:
        return batch_outputs


def decode_set(sess, model, dataset, FLAGS, verbose=True):
    grouped_dataset = data_utils.group_data_by_nl(dataset, use_bucket=True,
                                                  use_temp=False)
    vocabs = data_utils.load_vocab(FLAGS)
    rev_sc_vocab = vocabs[1]

    slot_filling_classifier = None
    if FLAGS.fill_argument_slots:
        # create slot filling classifier
        mapping_param_dir = os.path.join(FLAGS.model_dir,
                    'train.{}.mappings.X.Y.npz'.format(FLAGS.sc_vocab_size))
        train_X, train_Y = data_utils.load_slot_filling_data(mapping_param_dir)
        slot_filling_classifier = classifiers.KNearestNeighborModel(
            FLAGS.num_nn_slot_filling, train_X, train_Y)
        print('Slot filling classifier parameters loaded.')

    with DBConnection() as db:
        db.create_schema()
        db.remove_model(model.model_sig)

        sorted_sc_temps = sorted(grouped_dataset.keys(), key=lambda x:len(x))
        example_id = 0
        for sc_temp in sorted_sc_temps:
            example_id += 1
            sc_strs, tg_strs, scs, tgs, cm_fulls, tg_fulls = \
                grouped_dataset[sc_temp]
            assert(len(sc_strs) == len(tg_strs))
            assert(len(sc_strs) == len(scs))
            assert(len(sc_strs) == len(tgs))
            assert(len(sc_strs) == len(cm_fulls))
            assert(len(tgs) == len(tg_fulls))
            # print(rev_sc_vocab)
            sc_normalized_temp = ' '.join([rev_sc_vocab[i] for i in scs[0]])
            if verbose:
                print("Example {}:".format(example_id))
                print("(Orig) Source: " + sc_temp.strip())
                print("Source: " + sc_normalized_temp)
                for j in xrange(len(tg_strs)):
                    print("GT Target {}: {}".format(j+1, tg_strs[j].strip()))

            batch_outputs, output_logits = translate_fun(sc_temp, sess, model,
                vocabs, FLAGS, slot_filling_classifier=slot_filling_classifier)
            if FLAGS.tg_char:
                batch_outputs, batch_char_outputs = batch_outputs

            if FLAGS.token_decoding_algorithm == "greedy":
                tree, pred_cmd, outputs = batch_outputs[0]
                score = output_logits[0]
                print("{} ({})".format(pred_cmd, score))
                db.add_prediction(
                    model.model_sig, sc_temp, pred_cmd, float(score))
            elif FLAGS.token_decoding_algorithm == "beam_search":
                if batch_outputs:
                    top_k_predictions = batch_outputs[0]
                    if FLAGS.tg_char:
                        top_k_char_predictions = batch_char_outputs[0]
                    top_k_scores = output_logits[0]
                    for j in xrange(min(FLAGS.beam_size, 10,
                                        len(batch_outputs[0]))):
                        if len(top_k_predictions) <= j:
                            break
                        top_k_pred_tree, top_k_pred_cmd, top_k_outputs = \
                            top_k_predictions[j]
                        if verbose:
                            print("Prediction {}: {} ({}) ".format(
                                j+1, top_k_pred_cmd, top_k_scores[j]))
                            if FLAGS.tg_char:
                                print("Character-based prediction {}: {}".format(
                                    j+1, top_k_char_predictions[j]))
                        try:
                            db.add_prediction(model.model_sig, sc_temp,
                                top_k_pred_cmd, float(top_k_scores[j]),
                                update_mode=False)
                        except UnicodeEncodeError:
                            try:
                                db.add_prediction(model.model_sig, 
                                    sc_temp.encode('utf-8'),
                                    top_k_pred_cmd.encode('utf-8'), 
                                    float(top_k_scores[j]),
                                    update_mode=False)
                            except UnicodeDecodeError:
                                db.add_prediction(model.model_sig, '', '',
                                    float(top_k_scores[j]),
                                    update_mode=False)
                    print()
                else:
                    print("I'm very sorry, I can't translate this command at the moment.")

            # if attn_alignments is not None:
            #     if FLAGS.token_decoding_algorithm == "greedy":
            #         M = attn_alignments[batch_id, :, :]
            #     elif FLAGS.token_decoding_algorithm == "beam_search":
            #         M = attn_alignments[batch_id, 0, :, :]
            #     visualize_attn_alignments(M, sc, outputs, rev_sc_vocab, rev_tg_vocab,
            #         os.path.join(FLAGS.model_dir, "{}-{}.jpg".format(bucket_id, example_id)))


def visualize_attn_alignments(M, source, target, rev_sc_vocab, rev_tg_vocab, output_path):
    target_length, source_length = M.shape

    nl = [rev_sc_vocab[x] for x in source]
    cm = []
    for i, x in enumerate(target):
        cm.append(rev_tg_vocab[x])
        if rev_tg_vocab[x] == data_utils._EOS:
            break

    plt.clf()
    if len(target) == 0:
        i = 0
    plt.imshow(M[:i+1, :], interpolation='nearest', cmap=plt.cm.Blues)

    pad_size = source_length - len(nl)
    plt.xticks(xrange(source_length),
               [x.replace("$$", "") for x in reversed(
                   nl + [data_utils._PAD] * pad_size)],
               rotation='vertical')
    plt.yticks(xrange(len(cm)), [x.replace("$$", "") for x in cm],
               rotation='horizontal')

    plt.colorbar()

    plt.savefig(output_path, bbox_inches='tight')
