import os
import numpy as np
import time
import pickle
import tensorflow as tf
from tensorflow.python.client import device_lib

import sklearn.model_selection

from khan.training.trainer import Trainer, flatten_results
from khan.training.trainer_multi_gpu import TrainerMultiGPU, flatten_results
from khan.data.dataset import RawDataset

from data_utils import HARTREE_TO_KCAL_PER_MOL
from data_loaders import DataLoader
from concurrent.futures import ThreadPoolExecutor

import argparse


def get_available_gpus():
    local_device_protos = device_lib.list_local_devices()
    return [x.name for x in local_device_protos if x.device_type == 'GPU']

def main():
    avail_gpus = get_available_gpus()
    print("Available GPUs:", avail_gpus)

    print('os.environ:', os.environ)

    config = tf.ConfigProto(allow_soft_placement=True)
    with tf.Session(config=config) as sess: # must be at start to reserve GPUs

        parser = argparse.ArgumentParser(description="Run ANI1 neural net training.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)

        parser.add_argument('--fitted', default=False, action='store_true', help="Whether or use fitted energy corrections")
        parser.add_argument('--add_ffdata', default=True, action='store_true', help="Whether or not to add the forcefield data")
        parser.add_argument('--gpus', default=4, help="Number of gpus to use")
        parser.add_argument('--batch_size', default='256', help="How many training points to consider before calculating each gradient")
        parser.add_argument('--max_local_epoch_count', default='30', help="How many epochs to try each learning rate before reducing it")

        parser.add_argument('--work-dir', default='~/work', help="location where work data is dumped")
        parser.add_argument('--train-dir', default='~/ANI-1_release', help="location where work data is dumped")
        parser.add_argument('--restart', default=False, action='store_true', help="Whether to restart from the save dir")

        args = parser.parse_args()

        print("Arguments", args)

        ANI_TRAIN_DIR = args.train_dir
        ANI_WORK_DIR = args.work_dir
        GRAPH_DB_DIR = '/nfs/working/scidev/stevenso/learning/khan/graphdb_xyz/xyz/'

        CALIBRATION_FILE_TRAIN = os.path.join(ANI_TRAIN_DIR, "results_QM_M06-2X.txt")
        CALIBRATION_FILE_TEST = os.path.join(ANI_TRAIN_DIR, "gdb_11_cal.txt")
        ROTAMER_TRAIN_DIR = [ os.path.join(ANI_TRAIN_DIR, "rotamers/train"), os.path.join(ANI_TRAIN_DIR, "rotamers/test") ]
        ROTAMER_TEST_DIR = GRAPH_DB_DIR
        CHARGED_ROTAMER_TEST_DIR = os.path.join(ANI_TRAIN_DIR, "charged_rotamers_2")
        CCSDT_ROTAMER_TEST_DIR = os.path.join(ANI_TRAIN_DIR, "ccsdt_dataset")

        save_dir = os.path.join(ANI_WORK_DIR, "save")
        if os.path.isdir(save_dir) and not args.restart:
            print('save_dir', save_dir, 'exists and this is not a restart job')
            exit()
        batch_size = int(args.batch_size)
        use_fitted = args.fitted
        add_ffdata = args.add_ffdata
        data_loader = DataLoader(use_fitted)

        print("------------Load evaluation data--------------")
        
        pickle_file = "eval_data_xy.pickle"
        if os.path.isfile(pickle_file):
            print('Loading pickle from', pickle_file)
            rd_gdb11, rd_ffneutral_mo62x, ffneutral_groups_mo62x, rd_ffneutral_ccsdt, ffneutral_groups_ccsdt, rd_ffcharged_mo62x, ffcharged_groups_mo62x = pickle.load( open(pickle_file, "rb") )
        else:
            print('gdb11')
            rd_gdb11 = data_loader.load_gdb11(ANI_TRAIN_DIR, CALIBRATION_FILE_TEST)
            print('ff')
            rd_ffneutral_mo62x, ffneutral_groups_mo62x = data_loader.load_ff(ROTAMER_TEST_DIR)
            rd_ffneutral_ccsdt, ffneutral_groups_ccsdt = data_loader.load_ff(CCSDT_ROTAMER_TEST_DIR)
            rd_ffcharged_mo62x, ffcharged_groups_mo62x = data_loader.load_ff(CHARGED_ROTAMER_TEST_DIR)
            print('Pickling data...')
            pickle.dump( (rd_gdb11, rd_ffneutral_mo62x, ffneutral_groups_mo62x, rd_ffneutral_ccsdt, ffneutral_groups_ccsdt, rd_ffcharged_mo62x, ffcharged_groups_mo62x), open( pickle_file, "wb" ) )

        eval_names    = ["Neutral Rotamers", "Neutral Rotamers CCSDT", "Charged Rotamers"]
        eval_groups   = [ffneutral_groups_mo62x, ffneutral_groups_ccsdt, ffcharged_groups_mo62x]
        eval_datasets = [rd_ffneutral_mo62x, rd_ffneutral_ccsdt, rd_ffcharged_mo62x]

        # This training code implements cross-validation based training, whereby we determine convergence on a given
        # epoch depending on the cross-validation error for a given validation set. When a better cross-validation
        # score is detected, we save the model's parameters as the putative best found parameters. If after more than
        # max_local_epoch_count number of epochs have been run and no progress has been made, we decrease the learning
        # rate and restore the best found parameters.

        trainer = TrainerMultiGPU(sess, n_gpus=int(args.gpus))
        max_local_epoch_count = int(args.max_local_epoch_count)

        if os.path.exists(save_dir):
            print("Restoring existing model from", save_dir)
            trainer.load(save_dir)
            trainer.load_best_params()
            # TODo: allow changing learning rate here
            #trainer.learning_rate = tf.get_variable('learning_rate', tuple(), tf.float32, tf.constant_initializer(1e-3), trainable=False) # if restart should have different learning rate
        else: # initialize new random weights. If weights give crazy energies that might break convergence, just try again. 
            acceptable_start_rmse = 200.0
            for attempt_count in range(100):
                trainer.initialize() # initialize to random variables
                test_err = trainer.eval_abs_rmse(rd_ffneutral_mo62x)
                print('Initial error from random weights: %.1f kcal/mol, acceptable limit: %.1f' % (test_err, acceptable_start_rmse) )
                if test_err < acceptable_start_rmse:
                    break

        print("Evaluating Rotamer Errors:")

        for name, ff_data, ff_groups in zip(eval_names, eval_datasets, eval_groups):
            print(name, "abs/rel rmses: {0:.6f} kcal/mol | ".format(trainer.eval_abs_rmse(ff_data)) + "{0:.6f} kcal/mol".format(trainer.eval_eh_rmse(ff_data, ff_groups)))
        
        print("------------Load training data--------------")
        
        pickle_files = ["gdb8_fftrain_fftest_xy.pickle", "gdb8_graphdb_xy.pickle", "gdb8_xy.pickle", "gdb7_xy.pickle"]
        pickle_file = pickle_files[0]
        if os.path.isfile(pickle_file):
            print('Loading pickle from', pickle_file)
            Xs, ys = pickle.load( open(pickle_file, "rb") )
        else:
            if add_ffdata:
                ff_train_dir = ROTAMER_TRAIN_DIR
            else:
                ff_train_dir = None
            Xs, ys = data_loader.load_gdb8(ANI_TRAIN_DIR, CALIBRATION_FILE_TRAIN, ff_train_dir)
            print('Pickling data...')
            pickle.dump( (Xs, ys), open( pickle_file, "wb" ) )

        print("------------Starting Training--------------")

        train_ops = [
            trainer.global_epoch_count,
            trainer.learning_rate,
            trainer.local_epoch_count,
            trainer.unordered_l2s,
            trainer.train_op
        ]
        start_time = time.time()
        best_test_score = None
        while sess.run(trainer.learning_rate) > 1e-9:
            if best_test_score is None: # start testing
                # generate new train and validation split at every training round?
                X_train, X_test, y_train, y_test = sklearn.model_selection.train_test_split(Xs, ys, test_size=0.2) # stratify by UTT would be good to try here
                rd_train, rd_test = RawDataset(X_train, y_train), RawDataset(X_test,  y_test)
                print( 'n_train =', len(y_train), 'n_test =', len(y_test) )
                best_test_score = trainer.eval_abs_rmse(rd_test)

            while sess.run(trainer.local_epoch_count) < max_local_epoch_count:
                for step in range(1): # how many rounds to perform before checking test rmse
                    train_step_time = time.time()
                    train_results = trainer.feed_dataset(
                        rd_train,
                        shuffle=True,
                        target_ops=train_ops, # note: evaluation takes about as long as training for the same number of points, so it can be a waste to evaluate every time
                        batch_size=batch_size)
                    sess.run(trainer.max_norm_ops) # regularization by imposing maximum norm of weights. Should this run after every batch instead? 
                    train_abs_rmse = np.sqrt(np.mean(flatten_results(train_results, pos=3))) * HARTREE_TO_KCAL_PER_MOL
                    #print('train_abs_rmse: %f, %.2fs' % (train_abs_rmse, time.time()-train_step_time) )
                global_epoch = train_results[0][0]
                learning_rate = train_results[0][1]
                local_epoch_count = train_results[0][2]
                test_abs_rmse_time = time.time()
                test_abs_rmse = trainer.eval_abs_rmse(rd_test)
                print('test_abs_rmse_time', time.time()-test_abs_rmse_time )
                time_per_epoch = time.time() - start_time
                start_time = time.time()
                print(time.strftime("%Y-%m-%d %H:%M:%S"), 'tpe:', "{0:.2f}s,".format(time_per_epoch), 'g-epoch', global_epoch, 'l-epoch', local_epoch_count, 'lr', "{0:.0e}".format(learning_rate), \
                    'train/test abs rmse:', "{0:.2f} kcal/mol,".format(train_abs_rmse), "{0:.2f} kcal/mol".format(test_abs_rmse), end='')

                if test_abs_rmse < best_test_score:
                    trainer.save_best_params()
                    gdb11_abs_rmse = trainer.eval_abs_rmse(rd_gdb11)
                    print(' | gdb11 abs rmse', "{0:.2f} kcal/mol | ".format(gdb11_abs_rmse), end='')
                    for name, ff_data, ff_groups in zip(eval_names, eval_datasets, eval_groups):
                        print(name, "abs/rel rmses", "{0:.2f} kcal/mol,".format(trainer.eval_abs_rmse(ff_data)), \
                            "{0:.2f} kcal/mol | ".format(trainer.eval_eh_rmse(ff_data, ff_groups)), end='')

                    best_test_score = test_abs_rmse
                    sess.run([trainer.incr_global_epoch_count, trainer.reset_local_epoch_count])
                else:
                    sess.run([trainer.incr_global_epoch_count, trainer.incr_local_epoch_count])

                print('')
                trainer.save(save_dir)
                

            print("==========Decreasing learning rate==========")
            sess.run(trainer.decr_learning_rate)
            sess.run(trainer.reset_local_epoch_count)
            trainer.load_best_params()

    return


if __name__ == "__main__":
    main()

