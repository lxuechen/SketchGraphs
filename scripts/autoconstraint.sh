#!/bin/bash
python -m sketchgraphs_models.autoconstraint.train \
    --dataset_train sequence_data/sg_t16_train.npy \
    --batch_size 128 \
    --num_workers 8

