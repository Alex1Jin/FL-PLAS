import numpy as np
import logging
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import matplotlib.pyplot as plt

from dataLoader import *
from defenders import *
from attackers import *
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.metrics import classification_report, accuracy_score

import pandas as pd
import pdb
from scipy.stats.mstats import hmean
import sys

from torch.nn.utils import parameters_to_vector, vector_to_parameters
import time

logging.basicConfig()
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def vectorize_net(net):
    return torch.cat([p.view(-1) for p in net.parameters()])

def train(model, data_loader, device, criterion, optimizer):
    model.train()
    loss=0
    for batch_idx, (batch_x, batch_y) in enumerate(data_loader):
        batch_x, batch_y = batch_x.to(device), batch_y.long().to(device)
        optimizer.zero_grad()
        output = model(batch_x) # get predict label of batch_x
        loss = criterion(output, batch_y) # cross entropy loss
        loss.backward()
        optimizer.step()

        if batch_idx % 10 == 0:
            logger.info("loss: {}".format(loss))
    return model,loss


def test_model(model, data_loader, device, print_perform=False):
    model.eval()  # switch to eval status
    y_true = []
    y_predict = []
    for step, (batch_x, batch_y) in enumerate(data_loader):
        batch_x, batch_y = batch_x.to(device), batch_y.to(device)
        batch_y_predict = model(batch_x)
        batch_y_predict = torch.argmax(batch_y_predict, dim=1)
        y_predict.append(batch_y_predict)
        y_true.append(batch_y)

    y_true = torch.cat(y_true, 0)
    y_predict = torch.cat(y_predict, 0)
    if print_perform:
        print(classification_report(y_true.cpu(), y_predict.cpu(), target_names=data_loader.dataset.classes))

    return accuracy_score(y_true.cpu(), y_predict.cpu())


#### fed_avg
def fed_avg_aggregator(net_list, global_model_pre, device):

    net_avg = copy.deepcopy(net_list[0])
    #### observe parameters
    # net_glo_vec = vectorize_net(global_model_pre)
    # print("{}   :  {}".format(-1, net_glo_vec[10000:10010]))
    # for i in range(len(net_list)):
    #     net_vec = vectorize_net(net_list[i])
    #     print("{}   :  {}".format(i, net_vec[10000:10010]))

    whole_aggregator = []

    for p_index, p in enumerate(net_list[0].parameters()):
        # initial
        params_aggregator = torch.zeros(p.size()).to(device)
        for net_index, net in enumerate(net_list):
            params_aggregator = params_aggregator + 1/len(net_list) * list(net.parameters())[p_index].data
        whole_aggregator.append(params_aggregator)

    for param_index, p in enumerate(net_avg.parameters()):
        p.data = whole_aggregator[param_index]
    return net_avg
def layering_global(model_list,benign, device):
    whole_aggregator = []
    net_list=[]
    net_avg=copy.deepcopy(model_list[0])
    for i in benign:
        net_list.append(model_list[i])
    for p_index, p in enumerate(net_list[0].parameters()):
        # initial
        params_aggregator = torch.zeros(p.size()).to(device)
        for net_index, net in enumerate(net_list):
            params_aggregator = params_aggregator + 1/len(net_list) * list(net.parameters())[p_index].data
        whole_aggregator.append(params_aggregator)

    for param_index, p in enumerate(net_avg.parameters()):
        p.data = whole_aggregator[param_index]
    return net_avg

class ParameterContainer:
    def __init__(self, *args, **kwargs):
        self.hyper_params = None

    def run(self, client_model, *args, **kwargs):
        raise NotImplementedError()


class FederatedLearningTrainer(ParameterContainer):
    def __init__(self, arguments=None, *args, **kwargs):
        self.net_avg = arguments['net_avg']
        self.partition_strategy = arguments['partition_strategy']
        self.dir_parameter = arguments['dir_parameter']
        self.net_dataidx_map = arguments['net_dataidx_map']
        self.num_nets = arguments['num_nets']
        self.part_nets_per_round = arguments['part_nets_per_round']
        self.fl_round = arguments['fl_round']
        self.local_training_epoch = arguments['local_training_epoch']
        self.malicious_local_training_epoch = arguments['malicious_local_training_epoch']
        self.args_lr = arguments['args_lr']
        self.args_gamma = arguments['args_gamma']
        self.batch_size = arguments['batch_size']
        self.device = arguments['device']
        self.dataname = arguments["dataname"]
        self.num_class = arguments["num_class"]
        self.datadir = arguments["datadir"]
        self.model = arguments["model"]
        self.load_premodel = arguments["load_premodel"]
        self.save_model = arguments["save_model"]
        self.client_select = arguments["client_select"]
        self.test_data_ori_loader = arguments["test_data_ori_loader"]
        self.test_data_backdoor_loader = arguments["test_data_backdoor_loader"]
        self.criterion = nn.CrossEntropyLoss()
        self.malicious_ratio = arguments["malicious_ratio"]
        self.trigger_label = arguments["trigger_label"]
        self.semantic_label = arguments["semantic_label"]
        self.poisoned_portion = arguments["poisoned_portion"]
        self.backdoor_type = arguments["backdoor_type"]
        self.defense_method = arguments["defense_method"]
        if self.defense_method =='FL-PLAS':
            self.model_list=[copy.deepcopy(self.net_avg)for _ in range(self.num_nets)]
        self.cut = arguments["cut"]

    def run(self):

        fl_iter_list = []
        main_task_acc = []
        backdoor_task_acc = []
        client_chosen = []
        train_loader_list = []
        los_sum=[]
        drop_last=True
        if self.dataname=='cifar100':
            drop_last=True
        train_data, test_data = load_init_data(dataname=self.dataname, datadir=self.datadir)
        xmam_data = copy.deepcopy(train_data)

        ################################################################ distribute data to clients before training
        if self.backdoor_type == 'semantic':
            if self.dataname !='cifar10':
                logger.info("wrong backdoor type")
                sys.exit()
            dataidxs = self.net_dataidx_map[9999]
            clean_idx = self.net_dataidx_map[99991]
            poison_idx = self.net_dataidx_map[99992]
            train_data_loader_semantic = create_train_data_loader_semantic(train_data, self.batch_size, dataidxs,
                                                              clean_idx, poison_idx)
        if self.backdoor_type == 'edge-case':
            if self.dataname !='cifar10':
                logger.info("wrong backdoor type")
                sys.exit()
            train_data_loader_edge = get_edge_dataloader(self.datadir, self.batch_size)

        if self.defense_method == 'fltrust':
            # indices = [i for i in range(49900, 50000)]
            indices=[]
            ip=[0 for i in range(self.num_class)]
            for i in range(self.num_class):
                for j in range(len(train_data)):
                    if train_data[j][1]==i:
                        if ip[train_data[j][1]]!=10:
                            ip[train_data[j][1]]+=1
                            indices.append(j)
                        else:
                            break


            root_data = create_train_data_loader(self.dataname, train_data, self.trigger_label,
                                                   self.poisoned_portion, self.batch_size, indices,
                                                   malicious=False,drop_last=drop_last)



        for c in range(self.num_nets):

            if c < self.malicious_ratio * self.num_nets:
                if self.backdoor_type == 'none':
                    dataidxs = self.net_dataidx_map[c]
                    train_data_loader = create_train_data_loader(self.dataname, train_data, self.trigger_label,
                                                                 self.poisoned_portion, self.batch_size, dataidxs,
                                                                 malicious=False,drop_last=drop_last)

                elif self.backdoor_type == 'trigger':
                    dataidxs = self.net_dataidx_map[c]
                    train_data_loader  = create_train_data_loader(self.dataname, train_data, self.trigger_label,
                                                             self.poisoned_portion, self.batch_size, dataidxs,
                                                             malicious=True,drop_last=drop_last)


                elif self.backdoor_type == 'semantic':
                    train_data_loader = train_data_loader_semantic

                elif self.backdoor_type == 'edge-case':
                    train_data_loader = train_data_loader_edge

            else:
                dataidxs = self.net_dataidx_map[c]

                train_data_loader = create_train_data_loader(self.dataname, train_data, self.trigger_label,
                                                             self.poisoned_portion, self.batch_size, dataidxs,
                                                             malicious=False,drop_last=drop_last)

            train_loader_list.append(train_data_loader)



        ########################################################################################## multi-round training
        for flr in range(1, self.fl_round+1):

            norm_diff_collector = []  # for NDC-adaptive
            g_user_indices = []  # for krum and multi-krum
            malicious_num = 0  # for krum and multi-krum
            nets_list = [i for i in range(self.num_nets)]
            # output the information about data number of selected clients

            if self.client_select == 'fix-pool':
                selected_node_indices = np.random.choice(nets_list, size=self.part_nets_per_round, replace=False)
            elif self.client_select == 'fix-frequency':
                selected_node_mali = np.random.choice(nets_list[ :int(self.num_nets * self.malicious_ratio)],
                                            size=round(self.part_nets_per_round * self.malicious_ratio), replace=False)
                selected_node_mali = selected_node_mali.tolist()
                selected_node_benign = np.random.choice(nets_list[int(self.num_nets * self.malicious_ratio): ],
                                            size=round(self.part_nets_per_round * (1-self.malicious_ratio)), replace=False)
                selected_node_benign = selected_node_benign.tolist()
                selected_node_mali.extend(selected_node_benign)
                selected_node_indices = selected_node_mali

            num_data_points = [len(self.net_dataidx_map[i]) for i in selected_node_indices]
            net_data_number = [num_data_points[i] for i in range(self.part_nets_per_round)]
            logger.info("client data number: {}, FL round: {}".format(net_data_number, flr))

            # we need to reconstruct the net list at the beginning
            if self.defense_method=='FL-PLAS':
                net_list = [copy.deepcopy(self.model_list[i])for i in selected_node_indices]
            else:
                net_list = [copy.deepcopy(self.net_avg) for _ in range(self.part_nets_per_round)]

            logger.info("################## Starting fl round: {}".format(flr))

            ### for stealthy attack, we reserve previous global model
            if flr == 1:
                global_model_pre = copy.deepcopy(self.net_avg)
            else:
                pass

            # start the FL process

            for net_idx, net in enumerate(net_list):

                global_user_idx = selected_node_indices[net_idx]
                if global_user_idx < self.malicious_ratio * self.num_nets:

                    logger.info("$malicious$ Working on client: {}, which is Global user: {}".format(net_idx, global_user_idx))
                    for e in range(1, self.malicious_local_training_epoch + 1):
                        optimizer = optim.SGD(net.parameters(), lr=self.args_lr * self.args_gamma ** (flr - 1),
                                              momentum=0.9,
                                              weight_decay=1e-4)

                        for param_group in optimizer.param_groups:
                            logger.info("Effective lr in fl round: {} is {}".format(flr, param_group['lr']))

                    malicious_num += 1
                    g_user_indices.append(global_user_idx)
                else:

                    logger.info("@benign@ Working on client: {}, which is Global user: {}".format(net_idx, global_user_idx))
                    for e in range(1, self.local_training_epoch + 1):

                        optimizer = optim.SGD(net.parameters(), lr=self.args_lr * self.args_gamma ** (flr - 1),
                                                  momentum=0.9,
                                                  weight_decay=1e-4)
                        for param_group in optimizer.param_groups:
                            logger.info("Effective lr in fl round: {} is {}".format(flr, param_group['lr']))

                        _,los=train(net, train_loader_list[global_user_idx], self.device, self.criterion, optimizer)
                    g_user_indices.append(global_user_idx)


                ### calculate the norm difference between global model pre and the updated benign client model for DNC's norm-bound
                vec_global_model_pre = parameters_to_vector(list(global_model_pre.parameters()))
                vec_updated_client_model = parameters_to_vector(list(net.parameters()))
                norm_diff = torch.norm(vec_updated_client_model - vec_global_model_pre)
                logger.info("the norm difference between global model pre and the updated benign client model: {}".format(norm_diff))
                norm_diff_collector.append(norm_diff.item())

            ########################################################################################## attack process

            ########################################################################################## defense process
            if self.defense_method == "none":
                self.defender = None
                chosens = 'none'

            elif self.defense_method == "krum":

                self.defender = Krum(mode='krum', num_workers=self.part_nets_per_round, num_adv=malicious_num)
                net_list, _, chosens = self.defender.exec(client_models=net_list, global_model_pre=self.net_avg, num_dps=net_data_number,
                                                        g_user_indices=g_user_indices, device=self.device)


            elif self.defense_method == "multi-krum":
                if malicious_num > 0:
                    self.defender = Krum(mode='multi-krum', num_workers=self.part_nets_per_round, num_adv=malicious_num)
                    net_list, net_freq, chosens = self.defender.exec(client_models=net_list, global_model_pre=self.net_avg, num_dps=net_data_number,
                                                       g_user_indices=g_user_indices, device=self.device)

                else:
                    chosens = g_user_indices

            elif self.defense_method == "ndc":
                chosens = 'none'
                logger.info("@@@ Nom Diff Collector Mean: {}".format(np.mean(norm_diff_collector)))
                self.defender = WeightDiffClippingDefense(norm_bound=np.mean(norm_diff_collector))
                for net_idx, net in enumerate(net_list):
                    self.defender.exec(client_model=net, global_model=global_model_pre)

            elif self.defense_method == "rsa":
                chosens = 'none'
                self.defender = RSA()
                self.defender.exec(client_model=net_list, global_model=global_model_pre, flround=flr)

            elif self.defense_method == "rfa":
                chosens = 'none'
                self.defender = RFA()
                net_list = self.defender.exec(client_model=net_list, maxiter=5, eps=0.1, ftol=1e-5, device=self.device)

            elif self.defense_method == "weak-dp":
                chosens = 'none'
                self.defender = AddNoise(stddev=0.0005)
                for net_idx, net in enumerate(net_list):
                    self.defender.exec(client_model=net, device=self.device)

            elif self.defense_method == 'fltrust':
                chosens = 'none'
                self.defender = fltrust()
                self.net_avg = self.defender.exec(net_list=net_list, global_model=global_model_pre,
                                                  root_data=root_data, flr=flr, lr=self.args_lr, gamma=self.args_gamma,
                                                  net_num = self.part_nets_per_round, device=self.device)
            elif self.defense_method =='FL-PLAS':
                chosens = 'none'
                self.defender=layering()
                self.net_avg ,model_list= self.defender.exec(client_model=net_list,global_model=global_model_pre,cut=self.cut,device=self.device)
                whole_aggregator=[]
                for param_index,p in enumerate(self.net_avg.parameters()):
                    whole_aggregator.append(p.data)
                for i in range(self.num_nets):
                    for param_index,p in enumerate(self.model_list[i].parameters()):
                        if param_index>self.cut:
                            break
                        p.data = whole_aggregator[param_index]

                for i in range(len(model_list)):
                    self.model_list[selected_node_indices[i]]=copy.deepcopy(model_list[i])

            elif self.defense_method == 'flame':
                chosens = 'none'
                self.defender = flame()
                self.net_avg = self.defender.exec(global_model_pre=self.net_avg, client_model=net_list,
                                                  device=self.device)
            else:
                # NotImplementedError("Unsupported defense method !")
                pass

            ########################################################################################################

            #################################### after local training periods and defence process, we fedavg the nets
            global_model_pre = self.net_avg
            overall_acc=0.0
            backdoor_acc=0.0
            if self.defense_method=='krum' and self.malicious_ratio>0:
                interval=50
            interval=100
            # interval=1
            if self.defense_method == 'FL-PLAS':

                if flr % interval == 0:
                    out = []
                    benign = [i for i in range(int(self.malicious_ratio * self.num_nets), self.num_nets)]
                    for i in range(len(benign)):
                        out.append(test_model(self.model_list[benign[i]], self.test_data_ori_loader, self.device,
                                              print_perform=False))


                    logger.info(out)
                    for i in range(len(out)):
                        overall_acc += out[i]
                    overall_acc /= len(benign)

                    out = []
                    logger.info("=====Main task test accuracy=====: {}".format(overall_acc))
                    for i in range(len(benign)):
                        out.append(test_model(self.model_list[benign[i]], self.test_data_backdoor_loader, self.device,
                                              print_perform=False))
                    logger.info(out)
                    logger.info(test_model(self.model_list[0], self.test_data_ori_loader, self.device,
                                           print_perform=False))
                    logger.info(test_model(self.model_list[0], self.test_data_backdoor_loader, self.device,
                                           print_perform=False))
                    backdoor_acc = 0
                    for i in range(len(benign)):
                        backdoor_acc += out[i]

                    backdoor_acc /= len(benign)

                    logger.info("=====Backdoor task test accuracy=====: {}".format(backdoor_acc))
            else:

                if not self.defense_method in [ 'fltrust','flame']:
                    self.net_avg = fed_avg_aggregator(net_list, global_model_pre, device=self.device)


                if flr%interval==0:
                    v = torch.nn.utils.parameters_to_vector(self.net_avg.parameters())

                    logger.info("############ Averaged Model : Norm {}".format(torch.norm(v)))

                    logger.info("Measuring the accuracy of the averaged global model, FL round: {} ...".format(flr))

                    overall_acc = test_model(self.net_avg, self.test_data_ori_loader, self.device)

                    logger.info("=====Main task test accuracy=====: {}".format(overall_acc))

                    backdoor_acc = test_model(self.net_avg, self.test_data_backdoor_loader, self.device)
                    logger.info("=====Backdoor task test accuracy=====: {}".format(backdoor_acc))

            if self.save_model == True:
              if flr == self.fl_round:
                name="savedModel/{}_.pt".format(self.dataname)
                if self.poisoned_portion==0:
                    name=name+"poi"
                torch.save(self.net_avg.state_dict(),name)


            fl_iter_list.append(flr)
            main_task_acc.append(overall_acc)
            backdoor_task_acc.append(backdoor_acc)
            client_chosen.append(chosens)
        
        #################################################################################### save result to .csv
        df = pd.DataFrame({'fl_iter': fl_iter_list,
                            'main_task_acc': main_task_acc,
                            'backdoor_task_acc': backdoor_task_acc,
                            'the chosen ones': client_chosen,
                            # 'benign loss':los_sum,
                            })
        # print(len(los_sum))
        results_filename = '1-{}_2-{}_3-{}_4-{}_5-{}_6-{}_7-{}_8-{}_9-{}_10-{}_11-{}_12-{}_13-{}_14-{}_15-{}_16-{}' \
                           '_17-{}_18-{}_19-{}'.format(
            self.dataname,  #1
            self.partition_strategy,  #2
            self.dir_parameter,  #3
            self.args_lr,  #4
            self.fl_round,  #5
            self.local_training_epoch,  #6
            self.malicious_local_training_epoch,  #7
            self.malicious_ratio,  #8
            self.part_nets_per_round,  #9
            self.num_nets,  #10
            self.poisoned_portion,  #11
            self.trigger_label,  #12
            self.defense_method,  #13
            self.model,  #14
            self.load_premodel,  #15
            self.backdoor_type,  #16
            self.client_select,   #17
            self.semantic_label,   #18
            self.cut,#19
        )
        f=open('./ma.txt','a')
        f.write(str(overall_acc))
        f.close()
        f=open('./ba.txt','a')
        f.write(str(backdoor_acc))
        f.close()

        df.to_csv('result/{}.csv'.format(results_filename), index=False)
        logger.info("Wrote accuracy results to: {}".format(results_filename))



