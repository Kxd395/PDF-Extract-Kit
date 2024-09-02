import os
os.environ["TOKENIZERS_PARALLELISM"] = "false"
from get_data_utils import *
import numpy as np
from tqdm.auto import tqdm
from multiprocessing import Pool
from functools import partial
from torch.utils.data import Dataset, TensorDataset, DataLoader
from dataaccelerate import DataPrefetcher 
from scihub_pdf_dataset import MFRImageDataset,rec_collate_fn,deal_with_one_pdf,none_collate_fn,clean_pdf_path,Timers
import yaml
from torchvision import transforms
try:
    client=build_client()
except:
    client=None
eps=1e-7

import argparse
from modules.post_process import latex_rm_whitespace
import torch
def mfr_model_init2(weight_dir, device='cpu',batch_size=128):
    args = argparse.Namespace(cfg_path="modules/UniMERNet/configs/demo.yaml", options=None)
    import unimernet.tasks as tasks
    from unimernet.common.config import Config
    from unimernet.processors import load_processor
    cfg = Config(args)
    cfg.config.model.pretrained = os.path.join(weight_dir, "pytorch_model.bin")
    cfg.config.model.model_config.model_name = weight_dir
    cfg.config.model.tokenizer_config.path = weight_dir
    task = tasks.setup_task(cfg)
    model = task.build_model(cfg)
    model = model.to(device)
    vis_processor = load_processor('formula_image_eval', cfg.config.datasets.formula_rec_eval.vis_processor.eval)
    mfr_transform = transforms.Compose([vis_processor, ])
    def gpu_inference(model, imgs):
        return model.generate({'image': imgs})['pred_str']
    model.gpu_inference=gpu_inference
    return model, mfr_transform

def mfr_model_init(weight_dir, device='cpu',batch_size=128):
    from tensorrt_llm.runtime import MultimodalModelRunner
    from clean_unimernet import DonutTokenizer
    from transformers import NougatProcessor,NougatImageProcessor
    weight_dir ='/mnt/petrelfs/zhangtianning.di/projects/PDF-Extract-Kit/weights/unimernet_clean' 
    args  = argparse.Namespace(max_new_tokens=30, batch_size=batch_size, log_level='info', 
                               visual_engine_dir=f'examples/multimodal/trt_engines.b{batch_size}/vision_encoder/', 
                               visual_engine_name='model.engine', 
                               llm_engine_dir=f'examples/multimodal/trt_engines.b{batch_size}/unimernet/1-gpu/bfloat16', 
                               hf_model_dir=weight_dir, 
                               input_text=None, num_beams=1, top_k=1, top_p=0.0, 
                               temperature=1.0, repetition_penalty=1.0, 
                               run_profiling=False, profiling_iterations=20, 
                               check_accuracy=False, video_path=None, 
                               image_path=None, path_sep=',', 
                               enable_context_fmha_fp32_acc=None)

    tokenizer = DonutTokenizer(weight_dir)
    model     = MultimodalModelRunner(args)
    vis_processor = NougatProcessor.from_pretrained(weight_dir).image_processor

    def gpu_inference(model, processed_image, batch_size=args.batch_size):
        assert batch_size>=len(processed_image)
        need_padding = batch_size - len(processed_image)
        origin_length= len(processed_image)
        processed_image = torch.nn.functional.pad(processed_image,(0,0,0,0,0,0,0,need_padding)).contiguous()
        pre_prompt = ['Question: which city is this? Answer:']*len(processed_image)
        post_prompt= [None]*len(processed_image)
        decoder_input_ids = torch.IntTensor([[0]])
        decoder_input_ids = decoder_input_ids.repeat((batch_size, 1))
        max_new_tokens=30
        attention_mask=None
 

        output_text = model.generate(pre_prompt,
                                     post_prompt,
                                     processed_image,
                                     decoder_input_ids,
                                     max_new_tokens,
                                     attention_mask=attention_mask,
                                     warmup=False)
        output_text = output_text[:origin_length]
        output_text = [t[0] for t in output_text]
        return output_text
    model.gpu_inference=gpu_inference
    return model, vis_processor


class TensorDataset(Dataset):
    def __init__(self, img_list):
        self.img_list = img_list
    def __len__(self):
        return len(self.img_list)

    def __getitem__(self, idx):
        return idx, self.img_list[idx]
    
def deal_with_one_dataset(pdf_path, result_path,  mfr_model, mfr_transform, 
                          pdf_batch_size  =32,
                          image_batch_size=256,
                          num_workers=8,
                          partion_num = 1,
                          partion_idx = 0):
    images_dataset = MFRImageDataset(pdf_path,mfr_transform,partion_num = partion_num, partion_idx = partion_idx)
    data_to_save =  fast_deal_with_one_dataset(images_dataset,mfr_model,
                                               pdf_batch_size  =pdf_batch_size,
                          image_batch_size=image_batch_size,num_workers=num_workers)
    write_jsonl_to_path(data_to_save,result_path,images_dataset.client)


def fast_deal_with_one_dataset(images_dataset:MFRImageDataset,
                               mfr_model,
                          pdf_batch_size  =32,
                          image_batch_size=256,
                          num_workers=8):

    image_collecter   = DataLoader(images_dataset,batch_size=pdf_batch_size,collate_fn=none_collate_fn, 
                            num_workers=num_workers,pin_memory=False,
                            prefetch_factor=2)  
    location_to_mfr = {}

    for image_pool_list in tqdm(image_collecter,position=1,leave=True,desc="Images batch"):
        no_image_pdf_list = []
        locations     = []
        image_tensors = []
        for idx,(pdf_path, image_dict) in enumerate(image_pool_list):
            if len(image_dict)==0:
                no_image_pdf_list.append(pdf_path)
                continue
            for key,tensor in image_dict.items():
                locations.append(key)
                image_tensors.append(tensor)
        if len(image_tensors) == 0:
            #tqdm.write("no mfr result, skip")
            continue
        
        
        dataset          = TensorDataset(image_tensors)
        if len(dataset)<=image_batch_size:
            adapat_num_workers = 0
        elif len(dataset)<=2*image_batch_size:
            adapat_num_workers = 1
        else:
            adapat_num_workers = num_workers
        dataloader_group = DataLoader(dataset, batch_size=image_batch_size, num_workers=adapat_num_workers, pin_memory=True, pin_memory_device='cuda')
        featcher   = DataPrefetcher(dataloader_group,device='cuda')
        pbar  = tqdm(total=len(dataset),position=2,leave=False,desc="GPU batch")
        batch = featcher.next()
        indexes=[]
        mfr_res=[]
        while batch is not None:
            index, imgs = batch
            output = mfr_model.gpu_inference(mfr_model, imgs)
            mfr_res.extend(output)
            indexes.extend([t.item() for t in index])
            pbar.update(len(imgs))
            batch = featcher.next()
        assert len(mfr_res) == len(image_tensors)
        
        for index, latex in zip(indexes, mfr_res):
            location = locations[index]
            location_to_mfr[location] = latex_rm_whitespace(latex)

       

    patch_metadata_list = []
    for pdf_index, pdf_metadata in enumerate(tqdm(images_dataset.metadata)):
        pdf_path = clean_pdf_path(pdf_metadata['path'])
        patch_metadata = {'path':pdf_path,'doc_layout_result':[]}
        for pdf_page_metadata in pdf_metadata['doc_layout_result']:
            page_id = pdf_page_metadata['page_id']
            #print(pdf_page_metadata)
            this_line_pool = {'page_id':page_id, 'layout_dets':[]}
            for bbox_metadata in pdf_page_metadata['layout_dets']:
                if bbox_metadata['category_id'] not in [13,14]:continue
                category_id = bbox_metadata['category_id']
                bbox_id = tuple(bbox_metadata['poly'])
                location= (pdf_path,page_id,bbox_id)
                if location not in location_to_mfr:
                    print(f"WARNING: one page {location} is not regitered, usually it is because page load fail")
                    continue
                latex = location_to_mfr[location]
                this_line_pool['layout_dets'].append({'category_id':category_id, 'latex':latex})
            patch_metadata['doc_layout_result'].append(this_line_pool)
        patch_metadata_list.append(patch_metadata)
    return patch_metadata_list

if __name__ == "__main__":
    
    with open('configs/model_configs.yaml') as f:
        model_configs = yaml.load(f, Loader=yaml.FullLoader)
    device = model_configs['model_args']['device']
    image_batch_size=128
    mfr_model, mfr_transform = mfr_model_init(model_configs['model_args']['mfr_weight'], device=device, batch_size= image_batch_size)
    images_dataset = MFRImageDataset("part-66210c190659-012745.jsonl",mfr_transform)
    images_dataset[0]
    patch_metadata_list =  fast_deal_with_one_dataset(images_dataset,mfr_model,
                                               pdf_batch_size  =32,
                          image_batch_size=image_batch_size,num_workers=8)
    write_jsonj_to_path(patch_metadata_list, "test_result/result.mfr.test3.jsonl", None)
        