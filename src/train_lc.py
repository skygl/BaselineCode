import argparse
import os
os.environ["CUDA_VISIBLE_DEVICES"]="1"
import torch
device = torch.device("cuda:0")
from torch import nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader
import transformers
from transformers import RobertaTokenizer, RobertaForMaskedLM, RobertaModel
from transformers import BertTokenizer
from transformers import AdamW, get_linear_schedule_with_warmup
from tokenization_kobert import KoBertTokenizer
from torch.optim import Adam
import time

import random
from collections import Counter, defaultdict
import numpy as np
from sample_few_shot import get_label_dict
from finetune_model import RobertaNER, BertNER
from eval_util import batch_span_eval
from data import *
from torch.utils.tensorboard import SummaryWriter

def generate_batch(batch):
    text = [F.pad(torch.tensor(x[0]), (0,max_seq_len-len(x[0])), "constant", 1) for x in batch] # batch_size * max_seq_len 
    text = pad_sequence(text, batch_first = True)
    attention_mask = [torch.cat((torch.ones_like(torch.tensor(x[0])), torch.zeros(max_seq_len-len(x[0]), dtype=torch.int64)), dim=0)
        if len(x[0]) < max_seq_len else torch.ones_like(torch.tensor(x[0]))[:max_seq_len] for x in batch]
    attention_mask = pad_sequence(attention_mask, batch_first = True)
    label = [F.pad(torch.tensor(x[1]), (0,max_seq_len-len(x[1])), "constant", -100) for x in batch]
    label = pad_sequence(label, batch_first = True)
    orig_len = [len(x[0]) for x in batch]

    return text, attention_mask, label, orig_len

def generate_batch_unsup(batch):
    text = [F.pad(torch.tensor(x[0]), (0,max_seq_len-len(x[0])), "constant", 1) for x in batch] # batch_size * max_seq_len 
    text = pad_sequence(text, batch_first = True)
    attention_mask = [torch.cat((torch.ones_like(torch.tensor(x[0])), torch.zeros(max_seq_len-len(x[0]), dtype=torch.int64)), dim=0)
        if len(x[0]) < max_seq_len else torch.ones_like(torch.tensor(x[0]))[:max_seq_len] for x in batch]
    attention_mask = pad_sequence(attention_mask, batch_first = True)
    prob = [torch.tensor(x[1]) for x in batch]
    prob = pad_sequence(prob, batch_first = True)
    orig_len = [len(x[0]) for x in batch]

    return text, attention_mask, prob, orig_len

def train_func(processed_training_set, epoch, tokenizer, label_sentence_dicts, soft_kmeans, count_num = 0, unsup_data_iter = None):

    # Train the model
    # print("learning rate: ")
    train_loss = 0
    total_pred, total_gold, total_crct = 0.0, 0.0, 0.0
    dataset_chosen = []
    data = []
    for i,d in enumerate(processed_training_set):
        one_dataset = DataLoader(d, batch_size=BATCH_SIZE, collate_fn=generate_batch)
        data.extend(one_dataset)
        dataset_chosen.extend([i for x in range(len(one_dataset))])
    all_data_index = [i for i in range(len(dataset_chosen))]
    print("shuffling sentences")
    random.shuffle(all_data_index)
    # print("first 50 data:")
    # print(all_data_index[:50])
    
    model.train()
    print(f"total {len(all_data_index)} iters")
    for k in all_data_index:
        count_num += 1
        text, attention_mask, cls, orig_len = data[k]
        id2label = id2labels[dataset_chosen[k]]
        optimizer.zero_grad()
        outputs = []
        text_1, attention_mask_1, cls_1 = text.to(device), attention_mask.to(device), cls.to(device)
        cls_2 = cls.to(device)
        # for p,sent in enumerate(cls_2):
        #     for q,cl in enumerate(sent):
        #         if cl == 0 and np.random.random_sample() > 0.2:
        #             cls_2[p][q]=-100
        # print(cls_1[0])
        # print(cls_2[0])
        loss, output = model(text_1, attention_mask=attention_mask_1, labels=cls_2, dataset = dataset_chosen[k])
        loss.mean().backward()
        train_loss += loss.mean().item()
        outputs=output
        optimizer.step()
        preds = [[id2label[int(x)] for j,x in enumerate(y[1:orig_len[i]-1]) if int(cls[i][j + 1]) != -100] for i,y in enumerate(outputs)]
        gold = [[id2label[int(x)] for x in y[1:orig_len[i]-1] if int(x) != -100] for i,y in enumerate(cls)]
    
        bpred, bgold, bcrct, _, _, _ = batch_span_eval(preds, gold)
        total_pred += bpred
        total_gold += bgold
        total_crct += bcrct

        
        if count_num%200 == 0:
            print(f"batch: {count_num}/{int(num_training_steps/N_EPOCHS)} lr: {optimizer.param_groups[0]['lr']:.9f} loss: {loss.mean().item()/BATCH_SIZE:.9f}")
            
        # Adjust the learning rate
        scheduler.step()

        if unsup_data_iter is not None:
            # print(f"unsup batches in this step: {int(unsup_batch_num / len(all_data_index)  * (k + 1)) - int(unsup_batch_num / len(all_data_index) * k)}")
            for k1 in range(int(unsup_batch_num / len(all_data_index) * k), int(unsup_batch_num / len(all_data_index) * (k + 1))):
                text, attention_mask, t_prob, orig_len = next(unsup_data_iter)
                optimizer.zero_grad()
                text_1, attention_mask_1, t_prob1 = text.to(device), attention_mask.to(device), t_prob.to(device)
                loss, output = model.forward_unsup(text_1, attention_mask=attention_mask_1, dataset = 0, t_prob = t_prob1)
                loss = args.unsup_lr * loss
                loss.mean().backward()
                train_loss += loss.mean().item()
                outputs=output
                optimizer.step()
                scheduler.step()
            # for k1, (text, attention_mask, t_prob, orig_len) in enumerate(data_loader[unsup_batch_num / len(all_data_index) / N_EPOCHS * (epoch * len(all_data_index) + k ):unsup_batch_num / len(all_data_index) / N_EPOCHS * (epoch * len(all_data_index) + k + 1)]): 
            #     print(k1)


    microp = total_crct/total_pred if total_pred > 0 else 0
    micror = total_crct/total_gold if total_gold > 0 else 0
    microf1 = 2*microp*micror/(microp + micror) if (microp + micror) > 0 else 0

    return train_loss / train_num_data_point * BATCH_SIZE, microp, micror, microf1

def test(data_, epoch, label_sentence_dicts, soft_kmeans, test_all_data = True, finetune = False):
    val_loss = 0
    total_pred, total_gold, total_crct = 0.0, 0.0, 0.0
    total_pred_per_type, total_gold_per_type, total_crct_per_type = defaultdict(int), defaultdict(int), defaultdict(int)
    # data = DataLoader(data_, batch_size=256, collate_fn=generate_batch)
    # data = generate_episode(data_, epoch,  EPISODE_NUM, TEST_SUP_CLS_NUM, test_id2label, label_sentence_dict)
    if test_all_data:
        dataset_chosen = []
        data = []
        for i,d in enumerate(data_):
            one_dataset = DataLoader(d, batch_size=64, collate_fn=generate_batch)
            data.extend(one_dataset)
            dataset_chosen.extend([i for x in range(len(one_dataset))])
        # data = generate_testing_episode(data_, epoch+1, TEST_SUP_CLS_NUM, test_id2label, label_sentence_dict)
    else:
        data, _ = generate_episode(data_, epoch,  EPISODE_NUM, TEST_SUP_CLS_NUM, test_id2label, label_sentence_dict, use_multipledata = False)
    idx = 0
    f1ss = []
    pss = []
    rss = []
    # device = torch.device('cuda:0')
    new_model = model
    new_model.eval()
    # new_model = torch.load(model_name+str(epoch + 1)+'.pt', map_location=lambda storage, loc: storage.cuda(0))
    for j, (text, attention_mask, cls, orig_len) in enumerate(data): 
        id2label = id2labels[dataset_chosen[j]]
        with torch.no_grad():
            text_1, attention_mask_1, cls_1 = text.to(device), attention_mask.to(device).to(device), cls.to(device)
            # we use the same dataset for training and testing
            loss, outputs = new_model(text_1, attention_mask=attention_mask_1, labels=cls_1, dataset = dataset_chosen[j])
            val_loss += loss.mean().item()
        preds = [[id2label[int(x)] for j,x in enumerate(y[1:orig_len[i]-1]) if int(cls[i][j + 1]) != -100] for i,y in enumerate(outputs)]
        gold = [[id2label[int(x)] for x in y[1:orig_len[i]-1] if int(x) != -100] for i,y in enumerate(cls)]

        for pred in preds:
            for t,token in enumerate(pred):
                if len(token.split('I-')) == 2:
                    if t == 0:
                        pred[t] = 'O'
                        continue
                    else:
                        tag = token.split('I-')[1]
                        if len(pred[t-1]) == 1:
                            pred[t] = 'O'
                        else:
                            if tag != pred[t-1].split('-')[1]:
                                pred[t] = 'O'  


        bpred, bgold, bcrct, pred_span_per_type, gold_span_per_type, crct_span_per_type = batch_span_eval(preds, gold)
        total_pred += bpred
        total_gold += bgold
        total_crct += bcrct
        for x in pred_span_per_type:
            total_pred_per_type[x] += pred_span_per_type[x]
            total_gold_per_type[x] += gold_span_per_type[x]
            total_crct_per_type[x] += crct_span_per_type[x]

    microp = total_crct/total_pred if total_pred > 0 else 0
    micror = total_crct/total_gold if total_gold > 0 else 0
    microf1 = 2*microp*micror/(microp + micror) if (microp + micror) > 0 else 0
    microp_per_type, micror_per_type, microf1_per_type = {}, {}, {}
    for x in total_pred_per_type:
        microp_per_type[x] = total_crct_per_type[x]/total_pred_per_type[x] if total_pred_per_type[x] > 0 else 0
        micror_per_type[x] = total_crct_per_type[x]/total_gold_per_type[x] if total_gold_per_type[x] > 0 else 0
        microf1_per_type[x] = 2*microp_per_type[x]*micror_per_type[x]/(microp_per_type[x]+micror_per_type[x]) if (microp_per_type[x]+micror_per_type[x]) > 0 else 0

    return val_loss / test_num_data_point * 64, microp, micror, microf1, microp_per_type, micror_per_type, microf1_per_type

def get_prob(data_, id2labels):
    val_loss = 0
    total_pred, total_gold, total_crct = 0.0, 0.0, 0.0
    total_pred_per_type, total_gold_per_type, total_crct_per_type = defaultdict(int), defaultdict(int), defaultdict(int)
    # data = DataLoader(data_, batch_size=256, collate_fn=generate_batch)
    # data = generate_episode(data_, epoch,  EPISODE_NUM, TEST_SUP_CLS_NUM, test_id2label, label_sentence_dict)

    dataset_chosen = []
    data = []
    for i,d in enumerate(data_):
        one_dataset = DataLoader(d, batch_size=64, collate_fn=generate_batch)
        data.extend(one_dataset)
        dataset_chosen.extend([i for x in range(len(one_dataset))])
    prob = []

    model.eval()
    # new_model = torch.load(model_name+str(epoch + 1)+'.pt', map_location=lambda storage, loc: storage.cuda(0))
    for j, (text, attention_mask, cls, orig_len) in enumerate(data): 
        id2label = id2labels[dataset_chosen[j]]
        with torch.no_grad():
            text_1, attention_mask_1, cls_1 = text.to(device), attention_mask.to(device).to(device), cls.to(device)
            # we use the same dataset for training and testing
            loss, output, logits = model(text_1, attention_mask=attention_mask_1, labels=cls_1, dataset = dataset_chosen[j], output_logits=True)
            val_loss += loss.mean().item()
            prob.extend(logits)
        preds = [[id2label[int(x)] for j,x in enumerate(y[1:orig_len[i]-1]) if int(cls[i][j + 1]) != -100] for i,y in enumerate(output)]
        gold = [[id2label[int(x)] for x in y[1:orig_len[i]-1] if int(x) != -100] for i,y in enumerate(cls)]

        bpred, bgold, bcrct, pred_span_per_type, gold_span_per_type, crct_span_per_type = batch_span_eval(preds, gold)
        total_pred += bpred
        total_gold += bgold
        total_crct += bcrct
        for x in pred_span_per_type:
            total_pred_per_type[x] += pred_span_per_type[x]
            total_gold_per_type[x] += gold_span_per_type[x]
            total_crct_per_type[x] += crct_span_per_type[x]

    microp = total_crct/total_pred if total_pred > 0 else 0
    micror = total_crct/total_gold if total_gold > 0 else 0
    microf1 = 2*microp*micror/(microp + micror) if (microp + micror) > 0 else 0
          
    prob = pad_sequence(prob, batch_first = True)
    print("predicted probablity shape")
    print(prob.shape)
    microp = total_crct/total_pred if total_pred > 0 else 0
    micror = total_crct/total_gold if total_gold > 0 else 0
    microf1 = 2*microp*micror/(microp + micror) if (microp + micror) > 0 else 0
    print(f'\tPrec: {microp * 100:.1f}%(val)\t|\tRecall: {micror * 100:.1f}%(val)\t|\tF1: {microf1 * 100:.1f}%(val)')
    
    return prob

def str2bool(v):
    if isinstance(v, bool):
       return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def weights_init_custom(model):
    init_layers = [9, 10, 11]
    dense_names = ["query", "key", "value", "dense"]
    layernorm_names = ["LayerNorm"]
    for name, module in model.bert.named_parameters():
        if any(f".{i}." in name for i in init_layers):
            if any(n in name for n in dense_names):
                if "bias" in name:
                    module.data.zero_()
                elif "weight" in name:
                    module.data.normal_(mean=0.0, std=model.config.initializer_range)
            elif any(n in name for n in layernorm_names):
                if "bias" in name:
                    module.data.zero_()
                elif "weight" in name:
                    module.data.fill_(1.0)
    return model





if __name__ == "__main__":

    

    parser = argparse.ArgumentParser(description='main',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--datapath', default='dataset')
    parser.add_argument('--dataset', default='conll2003')
    parser.add_argument('--train_text', default='train.words')
    parser.add_argument('--train_ner', default='train.ner')
    parser.add_argument('--test_text', default='test.words')
    parser.add_argument('--test_ner', default='test.ner')
    parser.add_argument('--few_shot_sets', default=1, type=int)
    parser.add_argument('--unsup_text', default=None)
    parser.add_argument('--unsup_ner', default=None)
    parser.add_argument('--base_model', default='roberta', choices=['bert','roberta','kobert'])
    parser.add_argument('--epoch', default=5, type=int)
    parser.add_argument('--train_cls_num', default=4, type=int)
    parser.add_argument('--test_cls_num', default=18, type=int)
    parser.add_argument('--max_seq_len', type=int)
    parser.add_argument('--batch_size', type=int)
    parser.add_argument('--soft_kmeans', type=str2bool, nargs='?', const=True, default=False)
    parser.add_argument('--lr', default=5e-5,type=float)
    parser.add_argument('--unsup_lr', default=1.0,type=float)
    parser.add_argument('--warmup_proportion', default=0.1, type=float)
    parser.add_argument('--weight_decay', default=0.01, type=float)
    parser.add_argument('--use_truecase', type=str2bool, nargs='?', const=True, default=False)
    parser.add_argument('--local_rank',type=int)
    parser.add_argument('--use_gpu',default='',type=str)
    parser.add_argument('--data_size',default='',type=str)

    parser.add_argument('--model_name', default='save/conll_naiveft_bert_seq128_epoch')
    parser.add_argument('--load_model', type=str2bool, nargs='?', const=True, default=False)
    parser.add_argument('--reinit', type=str2bool, nargs='?', const=True, default=False)
    parser.add_argument('--load_model_name', default='save/conll_naiveft_bert_seq128_epoch')
    parser.add_argument('--load_checkpoint', type=str2bool, nargs='?', const=True, default=False)
    parser.add_argument('--load_dataset', type=str2bool, nargs='?', const=True, default=False)
    parser.add_argument('--train_dataset_file', default=None)
    parser.add_argument('--test_dataset_file', default=None)
    parser.add_argument('--label2ids', default=None)
    parser.add_argument('--id2labels', default=None)

    args = parser.parse_args()
    print(args)

    total_f1_scores = []
    st_f1_scores = []
    train_text_file = args.train_text
    train_ner_file = args.train_ner
    for round in range(args.few_shot_sets):
        if '.' not in args.train_text:
            args.train_text = train_text_file + f'_{round}.words'
            args.train_ner = train_ner_file + f'_{round}.ner'
        print(f"train text is {args.train_text}")
        datasets = args.dataset.split('_')
        print(datasets)
        train_texts = [os.path.join(args.datapath, dataset, args.train_text) for dataset in datasets]
        train_ners = [os.path.join(args.datapath, dataset, args.train_ner) for dataset in datasets]
        test_texts = [os.path.join(args.datapath, dataset, args.test_text) for dataset in datasets]
        test_ners = [os.path.join(args.datapath, dataset, args.test_ner) for dataset in datasets]

        max_seq_len = args.max_seq_len
        base_model = args.base_model

        if base_model == 'roberta':
            tokenizer = RobertaTokenizer.from_pretrained('roberta-base')
        elif base_model == 'bert':
            tokenizer = BertTokenizer.from_pretrained('bert-base-cased')
        elif base_model == 'kobert':
            tokenizer = KoBertTokenizer.from_pretrained('monologg/kobert')

        label2ids, id2labels = [], []
        processed_training_set, train_label_sentence_dicts, processed_test_set, test_label_sentence_dicts = [], [], [], []
        if not args.load_dataset:
            for train_text, train_ner, test_text, test_ner in zip(train_texts, train_ners, test_texts, test_ners):
                with open(train_ner) as fner, open(train_text) as f:
                    train_ner_tags, train_words = fner.readlines(), f.readlines() 
                with open(test_ner) as fner, open(test_text) as f:
                    test_ner_tags, test_words = fner.readlines(), f.readlines()      
                label2id, id2label = get_label_dict([train_ner_tags, test_ner_tags])
                
                label2ids.append(label2id)
                id2labels.append(id2label)

                train_ner_tags, train_words, train_label_sentence_dict = process_data(train_ner_tags, train_words, tokenizer, label2id, max_seq_len,base_model=base_model,use_truecase=args.use_truecase)
                test_ner_tags, test_words, test_label_sentence_dict = process_data(test_ner_tags, test_words, tokenizer, label2id, max_seq_len,base_model=base_model,use_truecase=args.use_truecase)

                sub_train_ = [[train_words[i], train_ner_tags[i]] for i in range(len(train_ner_tags))]
                sub_valid_ = [[test_words[i], test_ner_tags[i]] for i in range(len(test_ner_tags))] 

                train_label_sentence_dicts.append(train_label_sentence_dict)
                test_label_sentence_dicts.append(test_label_sentence_dict)

                # print([(x,len(train_label_sentence_dict[x])) for x in train_label_sentence_dict])
                processed_training_set.append(sub_train_) 
                processed_test_set.append(sub_valid_) 

        if args.load_dataset:
            start_time = time.time()
            print("start loading training dataset!")
            processed_training_set = np.load(args.train_dataset_file,allow_pickle=True)
            print("start loading test dataset!")
            processed_test_set = np.load(args.test_dataset_file,allow_pickle=True)
            print("start loading label ids!")
            label2ids = np.load(args.label2ids,allow_pickle=True)
            id2labels = np.load(args.id2labels,allow_pickle=True)
            secs = int(time.time() - start_time)
            mins = secs / 60
            secs = secs % 60
            print(f"finish loading datasets! | time in {mins} minutes, {secs} seconds")
            train_label_sentence_dict = None
            test_label_sentence_dict = None
            

        dataset_label_nums = [len(x) for x in label2ids]
        print(f"dataset label nums: {dataset_label_nums}")
        train_num_data_point = sum([len(sub_train_) for sub_train_ in processed_training_set])
        print(f"number of all training data points: {train_num_data_point}")
        test_num_data_point = sum([len(sub_train_) for sub_train_ in processed_test_set])
        print(f"number of all testing data points: {test_num_data_point}")



        LOAD_MODEL = args.load_model
        if LOAD_MODEL:
            if 'checkpoint' not in args.load_model_name:
                state = torch.load(args.load_model_name)
                if base_model == 'roberta':
                    model = RobertaNER.from_pretrained('roberta-base', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                elif base_model == 'bert':
                    model = BertNER.from_pretrained('bert-base-cased', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                elif base_model == 'kobert':
                    model = BertNER.from_pretrained('monologg/kobert', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                pretrained_dict = state.state_dict()
                model_dict = model.state_dict()
                pretrained_dict = {k:v for k,v in pretrained_dict.items() if k in model_dict and 'classifiers.0.' not in k} 
                model_dict.update(pretrained_dict)
                model.load_state_dict(model_dict)
                model.dataset_label_nums = dataset_label_nums
            else:
                state = torch.load(args.load_model_name)
                if base_model == 'roberta':
                    model = RobertaNER.from_pretrained('roberta-base', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                elif base_model == 'bert':
                    model = BertNER.from_pretrained('bert-base-cased', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                elif base_model == 'kobert':
                    model = BertNER.from_pretrained('monologg/kobert', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                pretrained_dict = state['state_dict']
                model_dict = model.state_dict()
                pretrained_dict = {k.split('module.')[1]:v for k,v in pretrained_dict.items() if k.split('module.')[1] in model_dict and 'classifier' not in k}
                print("pretrained dict")
                print(pretrained_dict)
                model_dict.update(pretrained_dict)
                model.load_state_dict(model_dict)
                # model.load_state_dict(state['state_dict'])
                print(model.config.hidden_size)
            model.classifiers = torch.nn.ModuleList([torch.nn.Linear(model.config.hidden_size, x) for x in dataset_label_nums])
        else:
            if base_model == 'roberta':
                model = RobertaNER.from_pretrained('roberta-base', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
            elif base_model == 'bert':
                model = BertNER.from_pretrained('bert-base-cased', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
            elif base_model == 'kobert':
                model = BertNER.from_pretrained('monologg/kobert', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
        if args.reinit:
            model = weights_init_custom(model)
        print("let's use ", torch.cuda.device_count(), "GPUs!")
        # torch.save(model, 'pretrained_models/lc_pretrained_190.pt')
        # exit(1)
        # model = torch.nn.DataParallel(model)
        model.to(device)
        model = torch.nn.DataParallel(model)
        # model = torch.nn.parallel.DistributedDataParallel(model,find_unused_parameters=True) # device_ids will include all GPU devices by default

        param_optimizer = list(model.named_parameters())

        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer
                        if not any(nd in n for nd in no_decay)], 'weight_decay': args.weight_decay},
            {'params': [p for n, p in param_optimizer
                        if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}]

        N_EPOCHS = args.epoch
        BATCH_SIZE = args.batch_size
        num_training_steps = N_EPOCHS * int(sum([len(sub_train_) for sub_train_ in processed_training_set]) / BATCH_SIZE)
        num_warmup_steps = int(args.warmup_proportion * num_training_steps)
        print(f"num training steps: {num_training_steps}")
        print(f"num warmup steps: {num_warmup_steps}")
        optimizer = AdamW(optimizer_grouped_parameters, lr=args.lr, correct_bias=True)
        # optimizer = Adam(optimizer_grouped_parameters, lr=args.lr, betas=(0.9,0.98), eps=1e-6)
        scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=num_warmup_steps, num_training_steps=num_training_steps)

        SOFT_KMEANS = args.soft_kmeans
        model_name = args.model_name

        start_epoch = 0
        LOAD_CHECKPOINT = args.load_checkpoint
        if LOAD_CHECKPOINT:
            print("start loading checkpoint")
            start_time = time.time()
            state = torch.load(args.load_model_name)

            # pretrained_dict = state['state_dict']
            # model_dict = model.state_dict()
            # pretrained_dict = {k:v for k,v in pretrained_dict.items() if k in model_dict and 'classifier' not in k}
            # model_dict.update(pretrained_dict)
            # model.load_state_dict(model_dict)

            model.load_state_dict(state['state_dict'])
            start_epoch = state['epoch']
            optimizer.load_state_dict(state['optimizer'])
            scheduler.load_state_dict(state['scheduler'])
            secs = int(time.time() - start_time)
            mins = secs / 60
            secs = secs % 60
            print(f"loaded from checkpoint - learning rate: {optimizer.param_groups[0]['lr']:.9f} | time in {mins} minutes, {secs} seconds")
            start_count_num = state['count_num']

        # with open(f"../results/naiveft_{args.dataset}_{args.data_size}.txt",'a') as fout:
        #     fout.write('\n')
        #     fout.write(str(args))
        #     fout.write('\n')

        for epoch in range(start_epoch, N_EPOCHS):

            start_time = time.time()
            if args.load_checkpoint and epoch == start_epoch:
                train_loss, microp, micror, microf1 = train_func(processed_training_set, epoch, tokenizer, train_label_sentence_dicts, soft_kmeans = SOFT_KMEANS, count_num = start_count_num)
            else:
                train_loss, microp, micror, microf1 = train_func(processed_training_set, epoch, tokenizer, train_label_sentence_dicts, soft_kmeans = SOFT_KMEANS)
            
            print(f'\tLoss: {train_loss:.4f}(train)\t|\tPrec: {microp * 100:.1f}%(train)\t|\tRecall: {micror * 100:.1f}%(train)\t|\tF1: {microf1 * 100:.1f}%(train)')
            # torch.save(model.module, model_name+str(epoch + 1)+'.pt')
            # writer.add_scalars( 'naiveft_'+args.dataset+'/'+model_name.split('/')[-1]+' (train)', {'F1-score': microf1, 'Precision': microp, 'Recall': micror}, epoch+1)
            # writer.add_scalar('naiveft_'+args.dataset+'/'+model_name.split('/')[-1]+' Loss (train)',train_loss,epoch+1)

            valid_loss, microp, micror, microf1, microp_per_type, micror_per_type, microf1_per_type = test(processed_test_set, epoch, test_label_sentence_dicts, soft_kmeans = SOFT_KMEANS)
            print(f'\tLoss: {valid_loss:.4f}(val)\t|\tPrec: {microp * 100:.1f}%(val)\t|\tRecall: {micror * 100:.1f}%(val)\t|\tF1: {microf1 * 100:.1f}%(val)')
            # print(f'microp per type: {microp_per_type} \nmicror_per_type: {micror_per_type} \nmicrof1_per_type: {microf1_per_type}')
            secs = int(time.time() - start_time)
            mins = secs / 60
            secs = secs % 60

            print('Epoch: %d' %(epoch + 1), " | time in %d minutes, %d seconds" %(mins, secs))
            # with open(f"../results/naiveft_{args.dataset}_{args.data_size}.txt",'a') as fout:
            #     fout.write(f"{microf1} ")
        
        total_f1_scores.append(microf1)
        # with open(f"results/naiveft_{args.dataset}_{args.data_size}_pretrain.txt",'a') as fout:
        #     fout.write("\n")
        # torch.save(model.module, 'teachers/'+args.train_text+'_ft.pt')


        if args.unsup_text is not None:

            print("###### Self-Training #####")
            # predict unlabeled data using all training examples and the fine-tuned model
            
            with open(os.path.join(args.datapath, args.dataset, args.unsup_ner)) as fner, open(os.path.join(args.datapath, args.dataset, args.unsup_text)) as f:
                unsup_ner_tags, unsup_words = fner.readlines(), f.readlines()
            # test_label2id, test_id2label = get_label_dict([test_ner_tags])
            unsup_ner_tags, unsup_words, _ = process_data(unsup_ner_tags, unsup_words, tokenizer, label2ids[0], max_seq_len,base_model=base_model)
            # use prediction instead of ground truth
            unsup_data_ = [[unsup_words[i], unsup_ner_tags[i]] for i in range(len(unsup_words))]
            print(f"unsup num: {len(unsup_data_)}")

            t_prob = get_prob([unsup_data_], id2labels)

            unsup_data_ = [[unsup_words[i], t_prob[i]] for i in range(len(unsup_words))]

            #load a pre-trained model and fine-tune again using the labeled and unlabeled data
            if LOAD_MODEL:
                if 'checkpoint' not in args.load_model_name:
                    state = torch.load(args.load_model_name)
                    if base_model == 'roberta':
                        model = RobertaNER.from_pretrained('roberta-base', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                    elif base_model == 'bert':
                        model = BertNER.from_pretrained('bert-base-cased', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                    elif base_model == 'kobert':
                        model = BertNER.from_pretrained('monologg/kobert', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                    pretrained_dict = state.state_dict()
                    model_dict = model.state_dict()
                    pretrained_dict = {k:v for k,v in pretrained_dict.items() if k in model_dict and 'classifiers.0.' not in k} 
                    model_dict.update(pretrained_dict)
                    model.load_state_dict(model_dict)
                    model.dataset_label_nums = dataset_label_nums
                else:
                    state = torch.load(args.load_model_name)
                    if base_model == 'roberta':
                        model = RobertaNER.from_pretrained('roberta-base', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                    elif base_model == 'bert':
                        model = BertNER.from_pretrained('bert-base-cased', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                    elif base_model == 'kobert':
                        model = BertNER.from_pretrained('monologg/kobert', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                    pretrained_dict = state['state_dict']
                    model_dict = model.state_dict()
                    pretrained_dict = {k.split('module.')[1]:v for k,v in pretrained_dict.items() if k.split('module.')[1] in model_dict and 'classifier' not in k}
                    print("pretrained dict")
                    print(pretrained_dict)
                    model_dict.update(pretrained_dict)
                    model.load_state_dict(model_dict)
                    # model.load_state_dict(state['state_dict'])
                    print(model.config.hidden_size)
                model.classifiers = torch.nn.ModuleList([torch.nn.Linear(model.config.hidden_size, x) for x in dataset_label_nums])
            else:
                if base_model == 'roberta':
                    model = RobertaNER.from_pretrained('roberta-base', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                elif base_model == 'bert':
                    model = BertNER.from_pretrained('bert-base-cased', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)
                elif base_model == 'kobert':
                    model = BertNER.from_pretrained('monologg/kobert', dataset_label_nums = dataset_label_nums, output_attentions=False, output_hidden_states=False, multi_gpus=True)

            if args.reinit:
                model = weights_init_custom(model)
            model.to(device)
            if torch.cuda.device_count() > 1:
                use_multi_gpus = True
                print("let's use ", torch.cuda.device_count(), "GPUs!")
                model = torch.nn.DataParallel(model)
                # model = torch.nn.parallel.DistributedDataParallel(model,find_unused_parameters=True)
            else:
                use_multi_gpus = False 
            gpu_num = torch.cuda.device_count()

            param_optimizer = list(model.named_parameters())

            no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
            optimizer_grouped_parameters = [
                {'params': [p for n, p in param_optimizer
                            if not any(nd in n for nd in no_decay)], 'weight_decay': args.weight_decay},
                {'params': [p for n, p in param_optimizer
                            if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}]

            N_EPOCHS = args.epoch
            BATCH_SIZE = args.batch_size
            num_training_steps = N_EPOCHS * int(sum([len(sub_train_) for sub_train_ in processed_training_set]) / BATCH_SIZE) + int(len(unsup_data_) / BATCH_SIZE / 1)
            num_warmup_steps = int(args.warmup_proportion * num_training_steps)
            print(f"num training steps: {num_training_steps}")
            print(f"num warmup steps: {num_warmup_steps}")
            optimizer = AdamW(optimizer_grouped_parameters, lr=args.lr, correct_bias=True)
            # optimizer = Adam(optimizer_grouped_parameters, lr=args.lr, betas=(0.9,0.98), eps=1e-6)
            scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=num_warmup_steps, num_training_steps=num_training_steps)

            if unsup_data_ is not None:
                unsup_batch_num = int(len(unsup_data_) / BATCH_SIZE / 1 / N_EPOCHS)
                print(f"unsup batches in one epoch: {int(len(unsup_data_) / BATCH_SIZE / 1 / N_EPOCHS)}")
                data_loader = DataLoader(unsup_data_, batch_size=1*BATCH_SIZE, collate_fn=generate_batch_unsup)
                print(f"all unsup batches: {len(data_loader)}")

            for epoch in range(N_EPOCHS):

                train_loss, microp, micror, microf1 = train_func(processed_training_set, epoch, tokenizer, train_label_sentence_dicts, soft_kmeans = SOFT_KMEANS, unsup_data_iter = iter(data_loader))
                
                
                print(f'\tLoss: {train_loss:.4f}(train)\t|\tPrec: {microp * 100:.1f}%(train)\t|\tRecall: {micror * 100:.1f}%(train)\t|\tF1: {microf1 * 100:.1f}%(train)')
                
                valid_loss, microp, micror, microf1, microp_per_type, micror_per_type, microf1_per_type = test(processed_test_set, epoch, test_label_sentence_dicts, soft_kmeans = SOFT_KMEANS)
                print(f'\tLoss: {valid_loss:.4f}(val)\t|\tPrec: {microp * 100:.1f}%(val)\t|\tRecall: {micror * 100:.1f}%(val)\t|\tF1: {microf1 * 100:.1f}%(val)')
                print(f'microp per type: {microp_per_type} \nmicror_per_type: {micror_per_type} \nmicrof1_per_type: {microf1_per_type}')
                secs = int(time.time() - start_time)
                mins = secs / 60
                secs = secs % 60

                print('Epoch: %d' %(epoch + 1), " | time in %d minutes, %d seconds" %(mins, secs))
                # with open(f"../results/naiveft_{args.dataset}_{args.data_size}.txt",'a') as fout:
                #     fout.write(f"{microf1} ")
            st_f1_scores.append(microf1)

            # with open(f'results/naiveft_{args.data_size}_{args.dataset}.txt','a') as fout:
            #     fout.write(f'\n')

    print(f"f1 scores: {total_f1_scores} \n average f1 scores: {sum(total_f1_scores)/len(total_f1_scores)}")
    if len(st_f1_scores) > 0:
        print(f"self-training f1 scores: {st_f1_scores} \n average self-training f1 scores: {sum(st_f1_scores)/len(st_f1_scores)}")
    


            


            