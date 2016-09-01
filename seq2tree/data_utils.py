# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Utilities for tokenizing & generating vocabularies."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import re
import sys
sys.path.append("../bashlex")

from data_tools import basic_tokenizer, bash_tokenizer, char_tokenizer, bash_parser
import normalizer

import tensorflow as tf

# Special vocabulary symbols - we always put them at the start.
_PAD = b"_PAD"
_EOS = b"_EOS"
_UNK = b"_UNK"
_ARG = b"ARGUMENT_UNK"
_UTL = b"HEADCOMMAND_UNK"
_FLAG = b"FLAG_UNK"
_NO_EXPAND = b"<NO_EXPAND>"

_GO = b"_GO"                # seq2seq start symbol
_ROOT = b"ROOT_"            # seq2tree start symbol

_NUM = b"_NUM"

_START_VOCAB = [_PAD, _EOS, _UNK, _ARG, _UTL, _FLAG, _NO_EXPAND, _GO, _ROOT]

PAD_ID = 0
EOS_ID = 1
UNK_ID = 2
ARG_ID = 3
UTL_ID = 4
FLAG_ID = 5
NO_EXPAND_ID = 6
GO_ID = 7
ROOT_ID = 8

# Regular expressions used to tokenize.
_DIGIT_RE = re.compile(br"\d")

def is_option(word):
    return word.startswith('-') or word.startswith("FLAG_")

def create_vocabulary(vocabulary_path, data, max_vocabulary_size,
                      tokenizer, base_tokenizer=None,
                      normalize_digits=True, normalize_long_pattern=True,
                      min_word_frequency=2):
    """Create vocabulary file (if it does not exist yet) from data file.

    Data file is assumed to contain one sentence per line. Each sentence is
    tokenized and digits are normalized (if normalize_digits is set).
    Vocabulary contains the most-frequent tokens up to max_vocabulary_size.
    We write it to vocabulary_path in a one-token-per-line format, so that later
    token in the first line gets id=0, second line gets id=1, and so on.

    Args:
      vocabulary_path: path where the vocabulary will be created.
      data: list of lines each of which corresponds to a data point.
      max_vocabulary_size: limit on the size of the created vocabulary.
      tokenizer: a function to use to tokenize each data sentence;
        if None, basic_tokenizer will be used.
      base_tokenizer: base_tokenizer used for separating a string into chars.
      normalize_digits: Boolean; if true, all digits are replaced by 0s.
      min_word_frequency: word frequency threshold below which a word is
        goint to be marked as _UNK.
    """
    if not tf.gfile.Exists(vocabulary_path):
        print("Creating vocabulary %s from data (%d)" % (vocabulary_path,
                                                         len(data)))
        vocab = {}
        counter = 0
        for line in data:
            counter += 1
            if counter % 1000 == 0:
                print("  processing line %d" % counter)
            if type(line) is list:
                tokens = line
            else:
                if base_tokenizer:
                    tokens = tokenizer(line, base_tokenizer, normalize_digits=normalize_digits,
                                       normalize_long_pattern=normalize_long_pattern)
                else:
                    tokens = tokenizer(line, normalize_digits=normalize_digits,
                                       normalize_long_pattern=normalize_long_pattern)
            if not tokens:
                continue
            for word in tokens:
                if word in vocab:
                    vocab[word] += 1
                else:
                    vocab[word] = 1

        sorted_vocab = {}
        for v in vocab:
            if vocab[v] >= min_word_frequency:
                sorted_vocab[v] = vocab[v]
            else:
                print("Infrequent token: %s"  % v)
        sorted_vocab = sorted(sorted_vocab, key=vocab.get, reverse=True)
        vocab_list = list(_START_VOCAB)
        for v in sorted_vocab:
            if not v in _START_VOCAB:
                vocab_list.append(v)

        if len(vocab_list) > max_vocabulary_size:
            vocab_list = vocab_list[:max_vocabulary_size]
        with tf.gfile.GFile(vocabulary_path, mode="wb") as vocab_file:
            for w in vocab_list:
                try:
                    vocab_file.write(w + b"\n")
                except Exception:
                    vocab_file.write(w.encode('utf-8') + b"\n")


def initialize_vocabulary(vocabulary_path):
    """Initialize vocabulary from file.

    We assume the vocabulary is stored one-item-per-line, so a file:
      dog
      cat
    will result in a vocabulary {"dog": 0, "cat": 1}, and this function will
    also return the reversed-vocabulary ["dog", "cat"].

    Args:
      vocabulary_path: path to the file containing the vocabulary.

    Returns:
      a pair: the vocabulary (a dictionary mapping string to integers), and
      the reversed vocabulary (a list, which reverses the vocabulary mapping).

    Raises:
      ValueError: if the provided vocabulary_path does not exist.
    """
    if tf.gfile.Exists(vocabulary_path):
        rev_vocab = []
        with tf.gfile.GFile(vocabulary_path, mode="rb") as f:
            rev_vocab.extend(f.readlines())
        rev_vocab = [line.strip() for line in rev_vocab]
        vocab = dict([(x, y) for (y, x) in enumerate(rev_vocab)])
        return vocab, rev_vocab
    else:
        raise ValueError("Vocabulary file %s not found.", vocabulary_path)


def token_ids_to_sentences(inputs, rev_vocab, headAppended=False):
    batch_size = len(inputs[0])
    sentences = []
    for i in xrange(batch_size):
        if headAppended:
            outputs = [decoder_input[i] for decoder_input in inputs[1:]]
        else:
            outputs = [decoder_input[i] for decoder_input in inputs]
        # If there is an EOS symbol in outputs, cut them at that point.
        if EOS_ID in outputs:
            outputs = outputs[:outputs.index(EOS_ID)]
        # If there is a PAD symbol in outputs, cut them at that point.
        if PAD_ID in outputs:
            outputs = outputs[:outputs.index(PAD_ID)]
        # Print out command corresponding to outputs.
        sentences.append(" ".join([tf.compat.as_str(rev_vocab[output])
                                   for output in outputs]))
    return sentences


def sentence_to_token_ids(sentence, vocabulary,
                          tokenizer, base_tokenizer,
                          normalize_digits=True,
                          normalize_long_pattern=True,
                          substitute_type=False):
    """Convert a string to list of integers representing token-ids.

    For example, a sentence "I have a dog" may become tokenized into
    ["I", "have", "a", "dog"] and with vocabulary {"I": 1, "have": 2,
    "a": 4, "dog": 7"} this function will return [1, 2, 4, 7].

    Args:
      sentence: the sentence in bytes format to convert to token-ids.
      vocabulary: a dictionary mapping tokens to integers.
      tokenizer: a function to use to tokenize each sentence;
        if None, basic_tokenizer will be used.
      normalize_digits: Boolean; if true, all digits are replaced by 0s.

    Returns:
      a list of integers, the token-ids for the sentence.
    """
    if type(sentence) is list:
        words = sentence
        substitute_type = True
    else:
        if base_tokenizer:
            words = tokenizer(sentence, base_tokenizer, normalize_digits=normalize_digits,
                             normalize_long_pattern=normalize_long_pattern)
        else:
            words = tokenizer(sentence, normalize_digits=normalize_digits,
                          normalize_long_pattern=normalize_long_pattern)

    token_ids = []
    for w in words:
        w = re.sub(_DIGIT_RE, _NUM, w) if normalize_digits and not is_option(w) else w
        if w in vocabulary:
            token_ids.append(vocabulary[w])
        else:
            if substitute_type:
                kind = w.split('_')[0].lower()
                if kind == "flag":
                    token_ids.append(FLAG_ID)
                elif kind == "headcommand":
                    token_ids.append(UTL_ID)
                else:
                    token_ids.append(ARG_ID)
            else:
                token_ids.append(UNK_ID)

    return token_ids


def data_to_token_ids(data, target_path, vocabulary_path,
                      tokenizer, base_tokenizer=None,
                      normalize_digits=True, normalize_long_pattern=True):
    """Tokenize data file and turn into token-ids using given vocabulary file.

    This function loads data line-by-line from data_path, calls the above
    sentence_to_token_ids, and saves the result to target_path. See comment
    for sentence_to_token_ids on the details of token-ids format.

    Args:
      data: list of lines each of which corresponds to a data point.
      target_path: path where the file with token-ids will be created.
      vocabulary_path: path to the vocabulary file.
      tokenizer: a function to use to tokenize each sentence;
        if None, basic_tokenizer will be used.
      base_tokenizer: base tokenizer used for splitting strings into characters.
      normalize_digits: Boolean; if true, all digits are replaced by 0s.
    """
    max_token_num = 0
    if not tf.gfile.Exists(target_path):
        print("Tokenizing data (%d)" % len(data))
        vocab, _ = initialize_vocabulary(vocabulary_path)
        tokens_file = tf.gfile.GFile(target_path, mode="w")
        counter = 0
        for line in data:
            counter += 1
            if counter % 1000 == 0:
                print("  tokenizing line %d" % counter)
            token_ids = sentence_to_token_ids(line, vocab, tokenizer, base_tokenizer,
                                              normalize_digits, normalize_long_pattern)
            if len(token_ids) > max_token_num:
                max_token_num = len(token_ids)
            tokens_file.write(" ".join([str(tok) for tok in token_ids])
                              + "\n")
        tokens_file.close()
    return max_token_num


def group_data_by_desp(dataset):
    grouped_dataset = {}

    for i in xrange(len(dataset)):
        nl_str, cm_str, nl, search_history = dataset[i]
        if nl_str in grouped_dataset:
            grouped_dataset[nl_str][0].append(cm_str)
            grouped_dataset[nl_str][2].append(search_history)
        else:
            grouped_dataset[nl_str] = [[cm_str], nl, [search_history]]

    grouped_dataset2 = []
    for nl_str in grouped_dataset:
        grouped_dataset2.append((nl_str, grouped_dataset[nl_str][0],
                                grouped_dataset[nl_str][1],
                                grouped_dataset[nl_str][2]))
    return grouped_dataset2


def prepare_data(data, data_dir, nl_vocab_size, cm_vocab_size):
    """Get data into data_dir, create vocabularies and tokenize data.

    Args:
      data: { 'train': [cm_list, nl_list],
              'dev': [cm_list, nl_list],
              'test': [cm_list, nl_list] }.
      data_dir: directory in which the data sets will be stored.
      nl_vocabulary_size: size of the English vocabulary to create and use.
      cm_vocabulary_size: size of the Command vocabulary to create and use.

    Returns:
      A tuple of 8 elements:
        (1) path to the token-ids for English training data-set,
        (2) path to the token-ids for Command training data-set,
        (3) path to the token-ids for English development data-set,
        (4) path to the token-ids for Command development data-set,
        (5) path to the token-ids for English test data-set,
        (6) path to the token-ids for Command test data-set,
        (7) path to the English vocabulary file,
        (8) path to the Command vocabulary file.
    """

    def add_to_set(data, nl_list, cm_list, cm_token_list, cm_seq_list):
        for nl, cmd in data:
            ast = bash_parser(cmd)
            if ast:
                if ast.is_simple():
                    cmd_tokens = normalizer.to_tokens(ast)
                    cmd_seq = normalizer.to_list(ast, list=[])
                    nl_list.append(nl)
                    cm_list.append(cmd)
                    cm_token_list.append(cmd_tokens)
                    cm_seq_list.append(cmd_seq)
                else:
                    print("Rare command: " + cmd.encode('utf-8'))

    train_cm_list = []
    train_cm_token_list = []
    train_cm_seq_list = []
    train_nl_list = []
    dev_cm_list = []
    dev_cm_token_list = []
    dev_cm_seq_list = []
    dev_nl_list = []
    test_cm_list = []
    test_cm_token_list = []
    test_cm_seq_list = []
    test_nl_list = []

    numFolds = len(data)
    for i in xrange(numFolds):
        if i < numFolds - 2:
            add_to_set(data[i], train_nl_list, train_cm_list, train_cm_token_list, train_cm_seq_list)
        elif i == numFolds - 2:
            add_to_set(data[i], dev_nl_list, dev_cm_list, dev_cm_token_list, dev_cm_seq_list)
        elif i == numFolds - 1:
            add_to_set(data[i], test_nl_list, test_cm_list, test_cm_token_list, test_cm_seq_list)

    global max_nl_token_len 
    max_nl_token_len = 0
    global max_nl_char_len 
    max_nl_char_len = 0
    global max_cm_char_len 
    max_cm_char_len = 0
    global max_cm_token_len 
    max_cm_token_len = 0
    global max_cm_seq_len 
    max_cm_seq_len = 0
    for cm_token in train_cm_token_list + dev_cm_token_list + test_cm_token_list:
        if len(cm_token) > max_cm_token_len:
            max_cm_token_len = len(cm_token)
    for cm_seq in train_cm_seq_list + dev_cm_seq_list + test_cm_seq_list:
        if len(cm_seq) > max_cm_seq_len:
            max_cm_seq_len = len(cm_seq)

    # Get data to the specified directory.
    train_path = os.path.join(data_dir, "train")
    dev_path = os.path.join(data_dir, "dev")
    test_path = os.path.join(data_dir, "test")

    # Create vocabularies of the appropriate sizes.
    cm_ast_vocab_path = os.path.join(data_dir, "vocab%d.cm.ast" % cm_vocab_size)
    cm_vocab_path = os.path.join(data_dir, "vocab%d.cm" % cm_vocab_size)
    cm_char_vocab_path = os.path.join(data_dir, "vocab%d.cm.char" % cm_vocab_size)
    nl_vocab_path = os.path.join(data_dir, "vocab%d.nl" % nl_vocab_size)
    nl_char_vocab_path = os.path.join(data_dir, "vocab%d.nl.char" % nl_vocab_size)
    create_vocabulary(cm_ast_vocab_path, train_cm_seq_list, cm_vocab_size, bash_tokenizer, True)
    create_vocabulary(cm_vocab_path, train_cm_token_list, cm_vocab_size, bash_tokenizer, True)
    create_vocabulary(cm_char_vocab_path, train_cm_list, cm_vocab_size, char_tokenizer,
                      normalize_digits=False, normalize_long_pattern=False)
    create_vocabulary(nl_vocab_path, train_nl_list, nl_vocab_size, basic_tokenizer, True)
    create_vocabulary(nl_char_vocab_path, train_nl_list, nl_vocab_size, char_tokenizer,
                      base_tokenizer=basic_tokenizer, normalize_digits=False,
                      normalize_long_pattern=False)

    def format_data(data_path, nl_list, cm_list, cm_token_list, cm_seq_list):
        cm_path = data_path + ".cm"
        nl_path = data_path + ".nl"
        with open(cm_path, 'w') as o_f:
            for cm in cm_list:
                o_f.write(cm.encode('utf-8') + '\n')
        with open(nl_path, 'w') as o_f:
            for nl in nl_list:
                o_f.write(nl.encode('utf-8') + '\n')
        cm_ids_path = data_path + (".ids%d.cm" % cm_vocab_size)
        cm_cids_path = data_path + (".cids%d.cm" % cm_vocab_size)
        cm_seq_path = data_path + (".seq%d.cm" % cm_vocab_size)
        nl_ids_path = data_path + (".ids%d.nl" % nl_vocab_size)
        nl_cids_path = data_path + (".cids%d.nl" % nl_vocab_size)
        temp = data_to_token_ids(cm_token_list, cm_ids_path, cm_vocab_path, bash_tokenizer)
        temp = data_to_token_ids(cm_list, cm_cids_path, cm_char_vocab_path, char_tokenizer,
                          None, normalize_digits=False, normalize_long_pattern=False)
        global max_cm_char_len
        if temp > max_cm_char_len:
            max_cm_char_len = temp
        temp = data_to_token_ids(cm_seq_list, cm_seq_path, cm_ast_vocab_path, bash_tokenizer)
        temp = data_to_token_ids(nl_list, nl_ids_path, nl_vocab_path, basic_tokenizer)
        global max_nl_token_len
        if temp > max_nl_token_len:
            max_nl_token_len = temp
        temp = data_to_token_ids(nl_list, nl_cids_path, nl_char_vocab_path, char_tokenizer,
                          basic_tokenizer, normalize_digits=False, normalize_long_pattern=False)
        global max_nl_char_len
        if temp > max_nl_char_len:
            max_nl_char_len = temp

    format_data(train_path, train_nl_list, train_cm_list, train_cm_token_list, train_cm_seq_list)
    format_data(dev_path, dev_nl_list, dev_cm_list, dev_cm_token_list, dev_cm_seq_list)
    format_data(test_path, test_nl_list, test_cm_list, test_cm_token_list, test_cm_seq_list)

    print("maximum num chars in description = %d" % max_nl_char_len)
    print("maximum num tokens in description = %d" % max_nl_token_len)
    print("maximum num chars in command = %d" % max_cm_char_len)
    print("maximum num tokens in command = %d" % max_cm_token_len)
    print("maximum AST construction step length = %d" % max_cm_seq_len)
