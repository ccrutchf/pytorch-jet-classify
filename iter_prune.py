import torch.nn as nn
import torch
import numpy as np
import pandas as pd
import models
import jet_dataset
import matplotlib.pyplot as plt
from optparse import OptionParser
from sklearn.metrics import accuracy_score, roc_curve, confusion_matrix, average_precision_score, auc, roc_auc_score
import torch.optim as optim
import torch.nn.utils.prune as prune
import yaml
import math
import seaborn as sn
import plot_weights
from pytorchtools import EarlyStopping
import copy
from datetime import datetime
import os
import os.path as path

def parse_config(config_file) :
    print("Loading configuration from", config_file)
    config = open(config_file, 'r')
    return yaml.load(config, Loader=yaml.FullLoader)


def countNonZeroWeights(model):
    nonzero = total = 0
    for name, p in model.named_parameters():
        tensor = p.data.cpu().numpy()
        nz_count = np.count_nonzero(tensor)
        total_params = np.prod(tensor.shape)
        nonzero += nz_count
        total += total_params
        print(f'{name:20} | nonzeros = {nz_count:7} / {total_params:7} ({100 * nz_count / total_params:6.2f}%) | total_pruned = {total_params - nz_count :7} | shape = {tensor.shape}')
    print(f'alive: {nonzero}, pruned : {total - nonzero}, total: {total}, Compression rate : {total/nonzero:10.2f}x  ({100 * (total-nonzero) / total:6.2f}% pruned)')
    return nonzero


def l1_regularizer(model, lambda_l1=0.01):
    #  after hours of searching, this man is a god: https://stackoverflow.com/questions/58172188/
    lossl1 = 0
    for model_param_name, model_param_value in model.named_parameters():
        if model_param_name.endswith('weight'):
            lossl1 += lambda_l1 * model_param_value.abs().sum()
    return lossl1


def train(model, optimizer, loss, train_loader, L1_factor=0.0001):
    train_losses = []
    model.to(device)
    model.mask_to_device(device)
    for i, data in enumerate(train_loader, 0):
        local_batch, local_labels = data
        model.train()
        local_batch, local_labels = local_batch.to(device), local_labels.to(device)
        # forward + backward + optimize
        optimizer.zero_grad()
        outputs = model(local_batch.float())
        criterion_loss = loss(outputs, local_labels.float())
        if options.l1reg:
            reg_loss = l1_regularizer(model, lambda_l1=L1_factor)
        else:
            reg_loss = 0
        total_loss = criterion_loss + reg_loss
        total_loss.backward()
        optimizer.step()
        step_loss = total_loss.item()
        train_losses.append(step_loss)
    return model, train_losses


def val(model, loss, val_loader, L1_factor=0.01):
    val_roc_auc_scores_list = []
    val_avg_precision_list = []
    val_losses = []
    model.to(device)
    with torch.set_grad_enabled(False):
        model.eval()
        for i, data in enumerate(val_loader, 0):
            local_batch, local_labels = data
            local_batch, local_labels = local_batch.to(device), local_labels.to(device)
            outputs = model(local_batch.float())
            criterion_loss = loss(outputs, local_labels.float())
            reg_loss = l1_regularizer(model, lambda_l1=L1_factor)
            val_loss = criterion_loss + reg_loss
            local_batch, local_labels = local_batch.cpu(), local_labels.cpu()
            outputs = outputs.cpu()
            val_roc_auc_scores_list.append(roc_auc_score(np.nan_to_num(local_labels.numpy()), np.nan_to_num(outputs.numpy())))
            val_avg_precision_list.append(average_precision_score(np.nan_to_num(local_labels.numpy()), np.nan_to_num(outputs.numpy())))
            val_losses.append(val_loss)
    return val_losses, val_avg_precision_list, val_roc_auc_scores_list


def test(model, test_loader, plot=True, pruned_params=0, base_params=0):
    predlist = torch.zeros(0, dtype=torch.long, device='cpu')
    lbllist = torch.zeros(0, dtype=torch.long, device='cpu')
    accuracy_score_value_list = []
    roc_auc_score_list = []
    model.to(device)
    with torch.no_grad():  # Evaulate pruned model performance
        for i, data in enumerate(test_loader):
            model.eval()
            local_batch, local_labels = data
            local_batch, local_labels = local_batch.to(device), local_labels.to(device)
            outputs = model(local_batch.float())
            _, preds = torch.max(outputs, 1)
            predlist = torch.cat([predlist, preds.view(-1).cpu()])
            lbllist = torch.cat([lbllist, torch.max(local_labels, 1)[1].view(-1).cpu()])
        outputs = outputs.cpu()
        local_labels = local_labels.cpu()
        predict_test = outputs.numpy()
        accuracy_score_value_list.append(accuracy_score(np.nan_to_num(lbllist.numpy()), np.nan_to_num(predlist.numpy())))
        roc_auc_score_list.append(roc_auc_score(np.nan_to_num(local_labels.numpy()), np.nan_to_num(outputs.numpy())))

        if plot:
            predict_test = outputs.numpy()
            df = pd.DataFrame()
            fpr = {}
            tpr = {}
            auc1 = {}

            #Time for filenames
            now = datetime.now()
            time = now.strftime("%d-%m-%Y_%H-%M-%S")

            # AUC/Signal Efficiency
            filename = 'ROC_{}b_{}_pruned_{}.png'.format(nbits,pruned_params,time)

            sig_eff_plt = plt.figure()
            sig_eff_ax = sig_eff_plt.add_subplot()
            for i, label in enumerate(test_dataset.labels_list):
                df[label] = local_labels[:, i]
                df[label + '_pred'] = predict_test[:, i]
                fpr[label], tpr[label], threshold = roc_curve(np.nan_to_num(df[label]), np.nan_to_num(df[label + '_pred']))
                auc1[label] = auc(np.nan_to_num(fpr[label]), np.nan_to_num(tpr[label]))
                plt.plot(np.nan_to_num(tpr[label]), np.nan_to_num(fpr[label]),
                         label='%s tagger, AUC = %.1f%%' % (label.replace('j_', ''), np.nan_to_num(auc1[label]) * 100.))
            sig_eff_ax.set_yscale('log')
            sig_eff_ax.set_xlabel("Signal Efficiency")
            sig_eff_ax.set_ylabel("Background Efficiency")
            sig_eff_ax.set_ylim(0.001, 1)
            sig_eff_ax.grid(True)
            sig_eff_ax.legend(loc='upper left')
            sig_eff_ax.text(0.25, 0.90, '(Pruned {} of {}, {}b)'.format(pruned_params,base_params,nbits),
                        fontweight='bold',
                        wrap=True, horizontalalignment='right', fontsize=12)
            sig_eff_plt.savefig(path.join(options.outputDir, filename))
            sig_eff_plt.show()
            plt.close(sig_eff_plt)

            # Confusion matrix
            filename = 'confMatrix_{}b_{}_pruned_{}.png'.format(nbits,pruned_params,time)
            conf_mat = confusion_matrix(np.nan_to_num(lbllist.numpy()), np.nan_to_num(predlist.numpy()))
            df_cm = pd.DataFrame(conf_mat, index=[i for i in test_dataset.labels_list],
                                 columns=[i for i in test_dataset.labels_list])
            plt.figure(figsize=(10, 7))
            sn.heatmap(df_cm, annot=True, fmt='g')
            plt.savefig(path.join(options.outputDir, filename))
            plt.show()
            plt.close()
    return accuracy_score_value_list, roc_auc_score_list


def prune_model(model, amount, prune_mask, method=prune.L1Unstructured):
    model.to('cpu')
    model.mask_to_device('cpu')
    for name, module in model.named_modules():  # re-apply current mask to the model
        if isinstance(module, torch.nn.Linear):
#            if name is not "fc4":
             prune.custom_from_mask(module, "weight", prune_mask[name])

    parameters_to_prune = (
        (model.fc1, 'weight'),
        (model.fc2, 'weight'),
        (model.fc3, 'weight'),
        (model.fc4, 'weight'),
    )
    prune.global_unstructured(  # global prune the model
        parameters_to_prune,
        pruning_method=method,
        amount=amount,
    )

    for name, module in model.named_modules():  # make pruning "permanant" by removing the orig/mask values from the state dict
        if isinstance(module, torch.nn.Linear):
#            if name is not "fc4":
            torch.logical_and(module.weight_mask, prune_mask[name],
                              out=prune_mask[name])  # Update progress mask
            prune.remove(module, 'weight')  # remove all those values in the global pruned model

    return model


def plot_metric_vs_bitparam(model_set,metric_results_set,bit_params_set,base_metrics_set,metric_text):
    # NOTE: Assumes that the first object in the base metrics set is the true base of comparison
    now = datetime.now()
    time = now.strftime("%d-%m-%Y_%H-%M-%S")

    filename = '{}_vs_bitparams'.format(metric_text) + str(time) + '.png'

    rel_perf_plt = plt.figure()
    rel_perf_ax = rel_perf_plt.add_subplot()

    for model, metric_results, bit_params in zip(model_set, metric_results_set, bit_params_set):
        nbits = model.weight_precision if hasattr(model, 'weight_precision') else 32
        rel_perf_ax.plot(bit_params, metric_results, linestyle='solid', marker='.', alpha=1, label='Pruned {}b'.format(nbits))

    #Plot "base"/unpruned model points
    for model, base_metric in zip(model_set,base_metrics_set):
        # base_metric = [[num_params],[base_metric]]
        nbits = model.weight_precision if hasattr(model, 'weight_precision') else 32
        rel_perf_ax.plot((base_metric[0] * nbits), 1/(base_metric[1]/base_metrics_set[0][1]), linestyle='solid', marker="X", alpha=1, label='Unpruned {}b'.format(nbits))

    rel_perf_ax.set_ylabel("1/{}/FP{}".format(metric_text,metric_text))
    rel_perf_ax.set_xlabel("Bit Params (Params * bits)")
    rel_perf_ax.grid(color='lightgray', linestyle='-', linewidth=1, alpha=0.3)
    rel_perf_ax.legend(loc='best')
    rel_perf_plt.savefig(path.join(options.outputDir, filename))
    rel_perf_plt.show()
    plt.close(rel_perf_plt)


def plot_total_loss(model_set, model_totalloss_set, model_estop_set):
    # Total loss over fine tuning
    now = datetime.now()
    time = now.strftime("%d-%m-%Y_%H-%M-%S")
    for model, model_loss, model_estop in zip(model_set, model_totalloss_set, model_estop_set):
        tloss_plt = plt.figure()
        tloss_ax = tloss_plt.add_subplot()
        nbits = model.weight_precision if hasattr(model, 'weight_precision') else 32
        filename = 'total_loss_{}b_{}.png'.format(nbits,time)
        tloss_ax.plot(range(1, len(model_loss[0]) + 1), model_loss[0], label='Training Loss')
        tloss_ax.plot(range(1, len(model_loss[1]) + 1), model_loss[1], label='Validation Loss')
        # plot each stopping point
        for stop in model_estop:
            tloss_ax.axvline(stop, linestyle='--', color='r', alpha=0.3)
        tloss_ax.set_xlabel('epochs')
        tloss_ax.set_ylabel('loss')
        tloss_ax.grid(True)
        tloss_ax.legend(loc='best')
        tloss_ax.set_title('Total Loss Across pruning & fine tuning {}b model'.format(nbits))
        tloss_plt.tight_layout()
        tloss_plt.savefig(path.join(options.outputDir,filename))
        tloss_plt.show()
        plt.close(tloss_plt)


if __name__ == "__main__":
    parser = OptionParser()
    parser.add_option('-i','--input'   ,action='store',type='string',dest='inputFile'   ,default='', help='location of data to train off of')
    parser.add_option('-o','--output'   ,action='store',type='string',dest='outputDir'   ,default='train_simple/', help='output directory')
    parser.add_option('-t','--test'   ,action='store',type='string',dest='test'   ,default='', help='Location of test data set')
    parser.add_option('-l','--load', action='store', type='string', dest='modelLoad', default=None, help='Model to load instead of training new')
    parser.add_option('-c','--config'   ,action='store',type='string',dest='config'   ,default='configs/train_config_threelayer.yml', help='tree name')
    parser.add_option('-e','--epochs'   ,action='store',type='int', dest='epochs', default=100, help='number of epochs to train for')
    parser.add_option('-p', '--patience', action='store', type='int', dest='patience', default=10,help='Early Stopping patience in epochs')
    parser.add_option('-L', '--lottery', action='store_true', dest='lottery', default=False, help='Prune and Train using the Lottery Ticket Hypothesis')
    parser.add_option('-a', '--no_bn_affine', action='store_false', dest='bn_affine', default=True, help='disable BN Affine Parameters')
    parser.add_option('-s', '--no_bn_stats', action='store_false', dest='bn_stats', default=True, help='disable BN running statistics')
    parser.add_option('-b', '--no_batnorm', action='store_false', dest='batnorm', default=True, help='disable BatchNormalization (BN) Layers ')
    parser.add_option('-r', '--no_l1reg', action='store_false', dest='l1reg', default=True, help='disable L1 Regularization totally ')
    (options,args) = parser.parse_args()
    yamlConfig = parse_config(options.config)
    #3938
    prune_value_set = [0.10, 0.111, .125, .143, .166, .20, .25, .333, .50, .666, .666,#take ~10% of the "original" value each time, reducing to ~15% original network size
                       0]  # Last 0 is so the final iteration can fine tune before testing

    if not path.exists(options.outputDir): #create given output directory if it doesnt exist
        os.makedirs(options.outputDir)

    prune_mask_set = [
        {  # Float Model
            "fc1": torch.ones(64, 16),
            "fc2": torch.ones(32, 64),
            "fc3": torch.ones(32, 32),
            "fc4": torch.ones(5, 32)},
        {  # Quant Model
            "fc1": torch.ones(64, 16),
            "fc2": torch.ones(32, 64),
            "fc3": torch.ones(32, 32),
            "fc4": torch.ones(5, 32)},
        {  # Quant Model
            "fc1": torch.ones(64, 16),
            "fc2": torch.ones(32, 64),
            "fc3": torch.ones(32, 32),
            "fc4": torch.ones(5, 32)},
        {  # Quant Model
            "fc1": torch.ones(64, 16),
            "fc2": torch.ones(32, 64),
            "fc3": torch.ones(32, 32),
            "fc4": torch.ones(5, 32)},
        {  # Quant Model
            "fc1": torch.ones(64, 16),
            "fc2": torch.ones(32, 64),
            "fc3": torch.ones(32, 32),
            "fc4": torch.ones(5, 32)},
    ]

    scaled_prune_mask_set = [
        {  # 1/4 Quant Model
            "fc1": torch.ones(16, 16),
            "fc2": torch.ones(8, 16),
            "fc3": torch.ones(8, 8)},
        {  # 4x Quant Model
            "fc1": torch.ones(256, 16),
            "fc2": torch.ones(128, 256),
            "fc3": torch.ones(128, 128)}
    ]
    # First model should be the "Base" model that all other accuracies are compared to!

    if options.lottery:
        # fix seed
        torch.manual_seed(yamlConfig["Seed"])
        torch.cuda.manual_seed_all(yamlConfig["Seed"]) #seeds all GPUs, just in case there's more than one
        np.random.seed(yamlConfig["Seed"])
    if options.batnorm:
        model_set = [models.three_layer_model_batnorm_masked(prune_mask_set[0], bn_affine=options.bn_affine, bn_stats=options.bn_stats), #32b
                     models.three_layer_model_bv_batnorm_masked(prune_mask_set[1],12, bn_affine=options.bn_affine, bn_stats=options.bn_stats), #12b
                     models.three_layer_model_bv_batnorm_masked(prune_mask_set[2],8, bn_affine=options.bn_affine, bn_stats=options.bn_stats), #8b
                     models.three_layer_model_bv_batnorm_masked(prune_mask_set[3],6, bn_affine=options.bn_affine, bn_stats=options.bn_stats), #6b
                     models.three_layer_model_bv_batnorm_masked(prune_mask_set[4],4, bn_affine=options.bn_affine, bn_stats=options.bn_stats)] #4x
    else:
        model_set = [models.three_layer_model_masked(prune_mask_set[0]), #32b
                     models.three_layer_model_bv_masked(prune_mask_set[1],12), #12b
                     models.three_layer_model_bv_masked(prune_mask_set[2],8), #8b
                     models.three_layer_model_bv_masked(prune_mask_set[3],6), #6b
                     models.three_layer_model_bv_masked(prune_mask_set[4],4)] #4x

    #save initalizations in case we're doing Lottery Ticket
    inital_models_sd = []
    for model in model_set:
        inital_models_sd.append(model.state_dict())


    print("# Models to train: {}".format(len(model_set)))
    # Sets for per-model Results/Data to plot
    prune_result_set = []
    prune_roc_set = []
    bit_params_set = []
    model_totalloss_set = []
    model_estop_set = []

    base_quant_accuracy_score, base_accuracy_score = None, None

    first_run = True
    first_quant = False

    # Setup cuda
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda:0" if use_cuda else "cpu")
    print("Using Device: {}".format(device))
    if use_cuda:
        print("cuda:0 device type: {}".format(torch.cuda.get_device_name(0)))

    if options.lottery:
        torch.backends.cudnn.enabled = True
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
    else:
        torch.backends.cudnn.enabled = True
        torch.backends.cudnn.benchmark = True
        torch.backends.cudnn.fastest = True

    # Set Batch size and split value
    batch_size = 1024
    train_split = 0.75

    # Setup and split dataset
    full_dataset = jet_dataset.ParticleJetDataset(options.inputFile,yamlConfig)
    test_dataset = jet_dataset.ParticleJetDataset(options.test, yamlConfig)
    train_size = int(train_split * len(full_dataset))  # 25% for Validation set, 75% for train set

    val_size = len(full_dataset) - train_size
    test_size = len(test_dataset)

    num_val_batches = math.ceil(val_size/batch_size)
    num_train_batches = math.ceil(train_size/batch_size)
    print("train_batches " + str(num_train_batches))
    print("val_batches " + str(num_val_batches))

    train_dataset, val_dataset = torch.utils.data.random_split(full_dataset,[train_size,val_size])

    print("train dataset size: " + str(len(train_dataset)))
    print("validation dataset size: " + str(len(val_dataset)))
    print("test dataset size: " + str(len(test_dataset)))


    # Setup dataloaders with our dataset
    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=batch_size,
                                              shuffle=True, num_workers=10, pin_memory=True)  # FFS, have to use numworkers = 0 because apparently h5 objects can't be pickled, https://github.com/WuJie1010/Facial-Expression-Recognition.Pytorch/issues/69

    val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=batch_size,
                                              shuffle=True, num_workers=10, pin_memory=True)
    test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=test_size,
                                              shuffle=False, num_workers=10, pin_memory=True)
    base_quant_params = None

    for model, prune_mask, init_sd in zip(model_set, prune_mask_set, inital_models_sd):
        # Model specific results/data to plot
        prune_results = []
        prune_roc_results = []
        bit_params = []
        model_loss = [[], []]  # Train, Val
        model_estop = []
        epoch_counter = 0
        pruned_params = 0
        for prune_value in prune_value_set:
            # Epoch specific plot values
            avg_train_losses = []
            avg_valid_losses = []
            val_roc_auc_scores_list = []
            avg_precision_scores = []
            accuracy_scores = []

            early_stopping = EarlyStopping(patience=options.patience, verbose=True)

            model.update_masks(prune_mask)  # Make sure to update the masks within the model

            optimizer = optim.Adam(model.parameters(), lr=0.0001)
            criterion = nn.BCELoss()

            L1_factor = 0.0001  # Default Keras L1 Loss
            estop = False

            if options.lottery:  # If using lottery ticket method, reset all weights to first initalized vals
                print("Resetting Model to Inital State dict with masks applied. Verifying via param count.")
                model.load_state_dict(init_sd)
                model.update_masks(prune_mask)
                model.force_mask_apply()
                countNonZeroWeights(model)

            for epoch in range(options.epochs):  # loop over the dataset multiple times
                epoch_counter += 1
                # Train
                model, train_losses = train(model, optimizer, criterion, train_loader, L1_factor=L1_factor)

                # Validate
                val_losses, val_avg_precision_list, val_roc_auc_scores_list = val(model, criterion, val_loader, L1_factor=L1_factor)

                # Calculate average epoch statistics
                try:
                    train_loss = np.average(train_losses)
                except:
                    train_loss = torch.mean(torch.stack(train_losses)).cpu().numpy()

                try:
                    valid_loss = np.average(val_losses)
                except:
                    valid_loss = torch.mean(torch.stack(val_losses)).cpu().numpy()

                val_roc_auc_score = np.average(val_roc_auc_scores_list)
                val_avg_precision = np.average(val_avg_precision_list)

                avg_train_losses.append(train_loss)
                avg_valid_losses.append(valid_loss)
                avg_precision_scores.append(val_avg_precision)

                # Print epoch statistics
                print('[epoch %d] train batch loss: %.7f' % (epoch + 1, train_loss))
                print('[epoch %d] val batch loss: %.7f' % (epoch + 1, valid_loss))
                print('[epoch %d] val ROC AUC Score: %.7f' % (epoch + 1, val_roc_auc_score))
                print('[epoch %d] val Avg Precision Score: %.7f' % (epoch + 1, val_avg_precision))

                # Check if we need to early stop
                early_stopping(valid_loss, model)
                if early_stopping.early_stop:
                    print("Early stopping")
                    estop = True
                    break

            # Load last/best checkpoint model saved via earlystopping
            model.load_state_dict(torch.load('checkpoint.pt'))

            # Time for plots
            now = datetime.now()
            time = now.strftime("%d-%m-%Y_%H-%M-%S")

            # Plot & save losses for this iteration
            loss_plt = plt.figure()
            loss_ax = loss_plt.add_subplot()

            loss_ax.plot(range(1, len(avg_train_losses) + 1), avg_train_losses, label='Training Loss')
            loss_ax.plot(range(1, len(avg_valid_losses) + 1), avg_valid_losses, label='Validation Loss')

            # find position of lowest validation loss
            if estop:
                minposs = avg_valid_losses.index(min(avg_valid_losses))
            else:
                minposs = options.epochs-1
            model_loss[0].extend(avg_train_losses[:minposs])
            model_loss[1].extend(avg_valid_losses[:minposs])

            # save position of estop overall app epochs
            model_estop.append(epoch_counter - ((len(avg_valid_losses)) - minposs))

            # update our epoch counter to represent where the model actually stopped training
            epoch_counter -= ((len(avg_valid_losses)) - minposs)

            # Plot losses for this iter

            loss_ax.axvline(minposs+1, linestyle='--', color='r', label='Early Stopping Checkpoint')
            loss_ax.set_xlabel('epochs')
            loss_ax.set_ylabel('loss')
            loss_ax.grid(True)
            loss_ax.legend()
            filename = 'loss_plot_e{}_{}_.png'.format(epoch_counter,time)
            loss_plt.savefig(path.join(options.outputDir, filename), bbox_inches='tight')
            loss_plt.show()
            plt.close(loss_plt)

            # Prune & Test model
            nbits = model.weight_precision if hasattr(model, 'weight_precision') else 32
            # Time for filenames
            now = datetime.now()
            time = now.strftime("%d-%m-%Y_%H-%M-%S")

            if first_run:
                # Test base model, first iteration of the float model
                print("Base Float Model:")
                base_params = countNonZeroWeights(model)
                accuracy_score_value_list, roc_auc_score_list = test(model, test_loader, pruned_params=0, base_params=base_params)
                base_accuracy_score = np.average(accuracy_score_value_list)
                base_roc_score = np.average(roc_auc_score_list)
                filename = path.join(options.outputDir, 'weight_dist_{}b_Base_{}.png'.format(nbits, time))
                plot_weights.plot_kernels(model, text=' (Unpruned FP Model)', output=filename)
                if not path.exists(path.join(options.outputDir,'models','{}b'.format(nbits))):
                    os.makedirs(path.join(options.outputDir,'models','{}b'.format(nbits)))
                model_filename = path.join(options.outputDir,'models','{}b'.format(nbits), "{}b_unpruned_{}.pth".format(nbits, time))
                torch.save(model.state_dict(),model_filename)
                first_run = False
            elif first_quant:
                # Test Unpruned, Base Quant model
                print("Base Quant Model: ")
                base_quant_params = countNonZeroWeights(model)
                accuracy_score_value_list, roc_auc_score_list = test(model, test_loader, pruned_params=0, base_params=base_quant_params)
                base_quant_accuracy_score = np.average(accuracy_score_value_list)
                base_quant_roc_score = np.average(roc_auc_score_list)
                filename = path.join(options.outputDir, 'weight_dist_{}b_qBase_{}.png'.format(nbits, time))
                plot_weights.plot_kernels(model,text=' (Unpruned Quant Model)', output=filename)
                if not path.exists(path.join(options.outputDir,'models','{}b'.format(nbits))):
                    os.makedirs(path.join(options.outputDir,'models','{}b'.format(nbits)))
                model_filename = path.join(options.outputDir,'models','{}b'.format(nbits), "{}b_unpruned_{}.pth".format(nbits, time))
                torch.save(model.state_dict(),model_filename)
                first_quant = False
            else:
                print("Pre Pruning:")
                current_params = countNonZeroWeights(model)
                accuracy_score_value_list, roc_auc_score_list = test(model, test_loader, pruned_params=(base_params-current_params), base_params=base_params)
                accuracy_score_value = np.average(accuracy_score_value_list)
                roc_auc_score_value = np.average(roc_auc_score_list)
                prune_results.append(1 / (accuracy_score_value / base_accuracy_score))
                prune_roc_results.append(1/ (roc_auc_score_value/ base_roc_score))
                bit_params.append(current_params * nbits)
                if not path.exists(path.join(options.outputDir,'models','{}b'.format(nbits))):
                    os.makedirs(path.join(options.outputDir,'models','{}b'.format(nbits)))
                model_filename = path.join(options.outputDir,'models','{}b'.format(nbits),"{}b_{}pruned_{}.pth".format(nbits, (base_params-current_params), time))
                torch.save(model.state_dict(),model_filename)

            # Prune for next iter
            if prune_value > 0:
                model = prune_model(model, prune_value, prune_mask)
                # Plot weight dist
                filename = path.join(options.outputDir, 'weight_dist_{}b_e{}_{}.png'.format(nbits, epoch_counter, time))
                print("Post Pruning: ")
                pruned_params = countNonZeroWeights(model)
                plot_weights.plot_kernels(model,
                                          text=' (Pruned ' + str(base_params - pruned_params) + ' out of ' + str(
                                              base_params) + ' params)',
                                          output=filename)

        if not first_quant and base_quant_accuracy_score is None:
            first_quant = True

        bit_params_set.append(bit_params)
        prune_result_set.append(prune_results)
        prune_roc_set.append(prune_roc_results)
        model_totalloss_set.append(model_loss)
        model_estop_set.append(model_estop)

    if base_quant_params == None:
        base_acc_set = [[base_params, base_accuracy_score]]
        base_roc_set = [[base_params, base_roc_score]]
    else:

        base_acc_set = [[base_params, base_accuracy_score],
                        [base_quant_params, base_quant_accuracy_score]]

        base_roc_set = [[base_params, base_roc_score],
                        [base_quant_params, base_quant_roc_score]]
    # Plot metrics
    plot_total_loss(model_set, model_totalloss_set, model_estop_set)
    plot_metric_vs_bitparam(model_set,prune_result_set,bit_params_set,base_acc_set,metric_text='ACC')
    plot_metric_vs_bitparam(model_set, prune_result_set, bit_params_set, base_roc_set, metric_text='ROC')
