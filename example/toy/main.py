#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
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

import os
import time
import numpy as np
import multiprocessing

import paddle
import paddle.fluid as F
import paddle.fluid.layers as L

from random import random
import jieba

import atarashi
import atarashi.data
from atarashi import log

# 你可以使用任何你喜欢的paddle框架，来构建网络. 比如PARL
#import parl.layers  as L

class Model(object):
    """
        model只需要定义
        `__init__`, `forward`, `loss`, `metrics`, `backward`
        让model跟写八股文一样
    """

    def __init__(self, config, mode):
        for k, v in config.items():
            log.info("%s: %s" % (k, repr(v)))
        self.hidden_size = config['hidden_size']
        self.vocab_size = config['vocab_size']
        self.embedding_size = config['embedding_size']
        self.num_layers = config['num_layers']

        self.learning_rate = config['learning_rate']
        self.mode = mode

    def forward(self, features):

        def FC(inputs, name, i, act):
            return L.fc(
                    inputs,
                    self.hidden_size,
                    act=act, 
                    param_attr=F.ParamAttr(name='%s.fc.w_%d' % (name, i), initializer=F.initializer.XavierInitializer(fan_in=self.hidden_size, fan_out=self.hidden_size)),
                    bias_attr=F.ParamAttr(name='%s.fc.b_%d' % (name, i), initializer=F.initializer.Constant(0.)))

        title_ids, comment_ids = features

        embedding_attr = F.ParamAttr(name='emb', initializer=F.initializer.XavierInitializer(fan_in=self.vocab_size, fan_out=self.embedding_size))

        title_encoded = L.embedding(title_ids, [self.vocab_size, self.embedding_size], param_attr=embedding_attr)
        comment_encoded = L.embedding(comment_ids, [self.vocab_size, self.embedding_size], param_attr=embedding_attr)

        # Vsum
        zero = L.fill_constant(shape=[1], dtype='int64', value=0)
        title_pad = L.cast(L.logical_not(L.equal(title_ids, zero)), 'float32')
        comment_pad = L.cast(L.logical_not(L.equal(comment_ids, zero)), 'float32')

        title_encoded = L.reduce_sum(title_encoded * title_pad, dim=1)
        title_encoded = L.softsign(title_encoded)
        comment_encoded = L.reduce_sum(comment_encoded * comment_pad, dim=1)
        comment_encoded = L.softsign(comment_encoded)


        for i in range(self.num_layers):
            title_encoded = FC(title_encoded, 'title', i, 'tanh')

        for i in range(self.num_layers):
            comment_encoded = FC(comment_encoded, 'comment', i, 'tanh')

        score = L.reduce_sum(title_encoded * comment_encoded, dim=1, keep_dim=True) / np.sqrt(self.hidden_size)
        if self.mode is atarashi.RunMode.PREDICT:
            probs = L.sigmoid(score)
            return probs
        else:
            return score

    def loss(self, predictions, labels):
        per_example_loss = L.sigmoid_cross_entropy_with_logits(predictions, L.cast(labels, 'float32')) 
        loss = L.reduce_mean(per_example_loss)
        return loss

    def backward(self, loss):
        optimizer = F.optimizer.AdamOptimizer(learning_rate=self.learning_rate)
        _, var_and_grads = optimizer.minimize(loss)
        return 

    def metrics(self, predictions, label):
        log.debug(label)
        auc = atarashi.metrics.Auc(label, L.sigmoid(predictions))
        acc = atarashi.metrics.Acc(label, L.unsqueeze(L.argmax(predictions, axis=1), axes=[1]))
        return {'acc': acc, 'auc': auc}


if __name__ == '__main__':
    parser = atarashi.ArgumentParser('DAN model with Paddle')
    parser.add_argument('--vocab_size', type=int, default=300000)
    parser.add_argument('--max_seqlen', type=int, default=128)
    parser.add_argument('--train_data_dir', type=str)
    parser.add_argument('--eval_data_dir', type=str)
    parser.add_argument('--vocab_file', type=str)

    args = parser.parse_args()
    run_config = atarashi.parse_runconfig(args)
    hparams = atarashi.parse_hparam(args)

    def jb_tokenizer(sen):
        return [s for s in jieba.cut(sen) if s != ' ']

    feature_column = atarashi.data.FeatureColumns([
            atarashi.data.TextColumn('title', vocab_file=args.vocab_file, tokenizer=jb_tokenizer),
            atarashi.data.TextColumn('comment', vocab_file=args.vocab_file, tokenizer=jb_tokenizer),
            atarashi.data.LabelColumn('label'),
        ])

    def before_batch(a, b, c):
        a = a[:args.max_seqlen]
        b = b[:args.max_seqlen]
        return a, b, c


    def after_batch(a, b, c):
        a = np.expand_dims(a, axis=-1)
        b = np.expand_dims(b, axis=-1)
        c = np.expand_dims(c, axis=-1)
        #if random() < 0.1:
        #    log.debug(a)
        #    log.debug(b)
        #    log.debug(c)
        return a, b, c

    train_ds = feature_column.build_dataset(data_dir=args.train_data_dir, shuffle=True, repeat=True) \
                                   .map(before_batch) \
                                   .padded_batch(run_config.batch_size, (0, 0, 0)) \
                                   .map(after_batch) 

    eval_ds = feature_column.build_dataset(data_dir=args.eval_data_dir, shuffle=False, repeat=False) \
                                   .map(before_batch) \
                                   .padded_batch(run_config.batch_size, (0, 0, 0)) \
                                   .map(after_batch) 

    shapes = ([-1, args.max_seqlen, 1], [-1, args.max_seqlen, 1], [-1, 1]) 
    types = ('int64', 'int64', 'int64')

    train_ds.output_shapes = shapes
    train_ds.output_types = types
    eval_ds.output_shapes = shapes
    eval_ds.output_types = types

    atarashi.train_and_eval(Model, hparams, run_config, train_ds, eval_ds)

