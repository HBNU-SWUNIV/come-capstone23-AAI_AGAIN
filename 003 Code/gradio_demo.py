import torch
from transformers import (
    LlamaForCausalLM,
    LlamaTokenizer,
    StoppingCriteria,
    BitsAndBytesConfig,
    AutoModelForCausalLM,
    AutoTokenizer
)
from transformers import AutoModel
model = AutoModel.from_pretrained("Teddysum/bllossom-1.01-13b", token="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")
import gradio as gr
import argparse
import os
from queue import Queue
from threading import Thread
import traceback
import gc
import json
import requests
from typing import Iterable, List
import subprocess
import re

import pygame
import os


# DEFAULT_SYSTEM_PROMPT = """You are a helpful assistant. 당신은 유능한 AI 어시스턴트 입니다."""
DEFAULT_SYSTEM_PROMPT = """You are a helpful assistant. 당신은 훌륭한 비서입니다."""

TEMPLATE_WITH_SYSTEM_PROMPT = (
    "[INST] <<SYS>>\n"
    "{system_prompt}\n"
    "<</SYS>>\n\n"
    "{instruction} [/INST]"
)

TEMPLATE_WITHOUT_SYSTEM_PROMPT = "[INST] {instruction} [/INST]"

# Parse command-line arguments
parser = argparse.ArgumentParser()
parser.add_argument(
    '--base_model',
    default=None,
    type=str,
    required=True,
    help='Base model path')
parser.add_argument('--lora_model', default=None, type=str,
                    help="If None, perform inference on the base model")
parser.add_argument(
    '--tokenizer_path',
    default=None,
    type=str,
    help='If None, lora model path or base model path will be used')
parser.add_argument(
    '--gpus',
    default="0",
    type=str,
    help='If None, cuda:0 will be used. Inference using multi-cards: --gpus=0,1,... ')
parser.add_argument('--share', default=True, help='Share gradio domain name')
parser.add_argument('--port', default=19324, type=int, help='Port of gradio demo')
parser.add_argument(
    '--max_memory',
    default=1024,
    type=int,
    help='Maximum number of input tokens (including system prompt) to keep. If exceeded, earlier history will be discarded.')
parser.add_argument(
    '--load_in_8bit',
    action='store_true',
    help='Use 8 bit quantized model')
parser.add_argument(
    '--load_in_4bit',
    action='store_true',
    help='Use 4 bit quantized model')
parser.add_argument(
    '--only_cpu',
    action='store_true',
    help='Only use CPU for inference')
parser.add_argument(
    '--alpha',
    type=str,
    default="1.0",
    help="The scaling factor of NTK method, can be a float or 'auto'. ")
parser.add_argument(
    "--use_vllm",
    action='store_true',
    help="Use vLLM as back-end LLM service.")
parser.add_argument(
    "--post_host",
    type=str,
    default="0.0.0.0",
    help="Host of vLLM service.")
parser.add_argument(
    "--post_port",
    type=int,
    default=8000,
    help="Port of vLLM service.")
args = parser.parse_args()

ENABLE_CFG_SAMPLING = True
try:
    from transformers.generation import UnbatchedClassifierFreeGuidanceLogitsProcessor
except ImportError:
    ENABLE_CFG_SAMPLING = False
    print("Install the latest transformers (commit equal or later than d533465) to enable CFG sampling.")
if args.use_vllm is True:
    print("CFG sampling is disabled when using vLLM.")
    ENABLE_CFG_SAMPLING = False

if args.only_cpu is True:
    args.gpus = ""
    if args.load_in_8bit or args.load_in_4bit:
        raise ValueError("Quantization is unavailable on CPU.")
if args.load_in_8bit and args.load_in_4bit:
    raise ValueError("Only one quantization method can be chosen for inference. Please check your arguments")
import sys
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(parent_dir)
from attn_and_long_ctx_patches import apply_attention_patch, apply_ntk_scaling_patch
if not args.only_cpu:
    apply_attention_patch(use_memory_efficient_attention=True)
apply_ntk_scaling_patch(args.alpha)

# Set CUDA devices if available
os.environ["CUDA_VISIBLE_DEVICES"] = args.gpus


# Peft library can only import after setting CUDA devices
from peft import PeftModel


# Set up the required components: model and tokenizer

def setup():
    global tokenizer, model, device, share, port, max_memory
    if args.use_vllm:
        # global share, port, max_memory
        max_memory = args.max_memory
        port = args.port
        share = args.share

        if args.lora_model is not None:
            raise ValueError("vLLM currently does not support LoRA, please merge the LoRA weights to the base model.")
        if args.load_in_8bit or args.load_in_4bit:
            raise ValueError("vLLM currently does not support quantization, please use fp16 (default) or unuse --use_vllm.")
        if args.only_cpu:
            raise ValueError("vLLM requires GPUs with compute capability not less than 7.0. If you want to run only on CPU, please unuse --use_vllm.")

        if args.tokenizer_path is None:
            args.tokenizer_path = args.base_model
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_path, legacy=True)

        print("Start launch vllm server.")
        cmd = f"python -m vllm.entrypoints.api_server \
            --model={args.base_model} \
            --tokenizer={args.tokenizer_path} \
            --tokenizer-mode=slow \
            --tensor-parallel-size={len(args.gpus.split(','))} \
            --host {args.post_host} \
            --port {args.post_port} \
            &"
        subprocess.check_call(cmd, shell=True)
    else:
        max_memory = args.max_memory
        port = args.port
        share = args.share
        load_type = torch.float16
        if torch.cuda.is_available():
            device = torch.device(0)
        else:
            device = torch.device('cpu')
        if args.tokenizer_path is None:
            args.tokenizer_path = args.lora_model
            if args.lora_model is None:
                args.tokenizer_path = args.base_model
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_path, legacy=True)

        base_model = AutoModelForCausalLM.from_pretrained(
            args.base_model,
            torch_dtype=load_type,
            low_cpu_mem_usage=True,
            device_map='auto',
            quantization_config=BitsAndBytesConfig(
                load_in_4bit=args.load_in_4bit,
                load_in_8bit=args.load_in_8bit,
                bnb_4bit_compute_dtype=load_type
            )
        )

        model_vocab_size = base_model.get_input_embeddings().weight.size(0)
        tokenizer_vocab_size = len(tokenizer)
        print(f"Vocab of the base model: {model_vocab_size}")
        print(f"Vocab of the tokenizer: {tokenizer_vocab_size}")
        if model_vocab_size != tokenizer_vocab_size:
            print("Resize model embeddings to fit tokenizer")
            base_model.resize_token_embeddings(tokenizer_vocab_size)
        if args.lora_model is not None:
            print("loading peft model")
            model = PeftModel.from_pretrained(
                base_model,
                args.lora_model,
                torch_dtype=load_type,
                device_map='auto',
            ).half()
        else:
            model = base_model

        if device == torch.device('cpu'):
            model.float()

        model.eval()


# Reset the user input
def reset_user_input():
    return gr.update(value='')


# Reset the state
def reset_state():
    return []


def generate_prompt(instruction, response="", with_system_prompt=True, system_prompt=DEFAULT_SYSTEM_PROMPT):
    if with_system_prompt is True:
        prompt = TEMPLATE_WITH_SYSTEM_PROMPT.format_map({'instruction': instruction,'system_prompt': system_prompt})
    else:
        prompt = TEMPLATE_WITHOUT_SYSTEM_PROMPT.format_map({'instruction': instruction})
    if len(response)>0:
        prompt += " " + response
    return prompt


# User interaction function for chat
def user(user_message, history):
    return gr.update(value="", interactive=False), history + \
        [[user_message, None]]


class Stream(StoppingCriteria):
    def __init__(self, callback_func=None):
        self.callback_func = callback_func

    def __call__(self, input_ids, scores) -> bool:
        if self.callback_func is not None:
            self.callback_func(input_ids[0])
        return False


class Iteratorize:
    """
    Transforms a function that takes a callback
    into a lazy iterator (generator).

    Adapted from: https://stackoverflow.com/a/9969000
    """
    def __init__(self, func, kwargs=None, callback=None):
        self.mfunc = func
        self.c_callback = callback
        self.q = Queue()
        self.sentinel = object()
        self.kwargs = kwargs or {}
        self.stop_now = False

        def _callback(val):
            if self.stop_now:
                raise ValueError
            self.q.put(val)

        def gentask():
            try:
                ret = self.mfunc(callback=_callback, **self.kwargs)
            except ValueError:
                pass
            except Exception:
                traceback.print_exc()

            clear_torch_cache()
            self.q.put(self.sentinel)
            if self.c_callback:
                self.c_callback(ret)

        self.thread = Thread(target=gentask)
        self.thread.start()

    def __iter__(self):
        return self

    def __next__(self):
        obj = self.q.get(True, None)
        if obj is self.sentinel:
            raise StopIteration
        else:
            return obj

    def __del__(self):
        clear_torch_cache()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_now = True
        clear_torch_cache()


def clear_torch_cache():
    gc.collect()
    if torch.cuda.device_count() > 0:
        torch.cuda.empty_cache()


def post_http_request(prompt: str,
                      api_url: str,
                      n: int = 1,
                      top_p: float = 0.9,
                      top_k: int = 40,
                      temperature: float = 0.2,
                      max_tokens: int = 512,
                      presence_penalty: float = 1.0,
                      use_beam_search: bool = False,
                      stream: bool = False) -> requests.Response:
    headers = {"User-Agent": "Test Client"}
    pload = {
        "prompt": prompt,
        "n": n,
        "top_p": 1 if use_beam_search else top_p,
        "top_k": -1 if use_beam_search else top_k,
        "temperature": 0 if use_beam_search else temperature,
        "max_tokens": max_tokens,
        "use_beam_search": use_beam_search,
        "best_of": 5 if use_beam_search else n,
        "presence_penalty": presence_penalty,
        "stream": stream,
    }
    print(pload)

    response = requests.post(api_url, headers=headers, json=pload, stream=True)
    return response


def get_streaming_response(response: requests.Response) -> Iterable[List[str]]:
    for chunk in response.iter_lines(chunk_size=8192,
                                     decode_unicode=False,
                                     delimiter=b"\0"):
        if chunk:
            data = json.loads(chunk.decode("utf-8"))
            output = data["text"]
            yield output


# Perform prediction based on the user input and history
@torch.no_grad()
def predict(
    history,
    system_prompt,
    negative_prompt,
    max_new_tokens=128,
    top_p=0.9,
    temperature=0.2,
    top_k=40,
    do_sample=True,
    repetition_penalty=1.1,
    guidance_scale=1.0,
    presence_penalty=0.0,
):
    if len(system_prompt) == 0:
        system_prompt = DEFAULT_SYSTEM_PROMPT
    while True:
        print("len(history):", len(history))
        print("history: ", history)
        history[-1][1] = ""
        if len(history) == 1:
            input = history[0][0]
            prompt = generate_prompt(input,response="", with_system_prompt=True, system_prompt=system_prompt)
        else:
            input = history[0][0]
            response = history[0][1]
            prompt = generate_prompt(input, response=response, with_system_prompt=True, system_prompt=system_prompt)+'</s>'
            for hist in history[1:-1]:
                input = hist[0]
                response = hist[1]
                prompt = prompt + '<s>'+generate_prompt(input, response=response, with_system_prompt=False)+'</s>'
            input = history[-1][0]
            prompt = prompt + '<s>'+generate_prompt(input, response="", with_system_prompt=False)

        input_length = len(tokenizer.encode(prompt, add_special_tokens=True))
        print(f"Input length: {input_length}")
        if input_length > max_memory and len(history) > 1:
            print(f"The input length ({input_length}) exceeds the max memory ({max_memory}). The earlier history will be discarded.")
            history = history[1:]
            print("history: ", history)
        else:
            break

    if args.use_vllm:
        generate_params = {
            'max_tokens': max_new_tokens,
            'top_p': top_p,
            'temperature': temperature,
            'top_k': top_k,
            "use_beam_search": not do_sample,
            'presence_penalty': presence_penalty,
        }

        api_url = f"http://{args.post_host}:{args.post_port}/generate"


        response = post_http_request(prompt, api_url, **generate_params, stream=True)

        for h in get_streaming_response(response):
            for line in h:
                line = line.replace(prompt, '')
                history[-1][1] = line
                yield history

    else:
        negative_text = None
        if len(negative_prompt) != 0:
            negative_text = re.sub(r"<<SYS>>\n(.*)\n<</SYS>>", f"<<SYS>>\n{negative_prompt}\n<</SYS>>", prompt)
        inputs = tokenizer(prompt, return_tensors="pt")
        input_ids = inputs["input_ids"].to(device)
        if negative_text is None:
            negative_prompt_ids = None
            negative_prompt_attention_mask = None
        else:
            negative_inputs = tokenizer(negative_text,return_tensors="pt")
            negative_prompt_ids = negative_inputs["input_ids"].to(device)
            negative_prompt_attention_mask = negative_inputs["attention_mask"].to(device)
        generate_params = {
            'input_ids': input_ids,
            'max_new_tokens': max_new_tokens,
            'top_p': top_p,
            'temperature': temperature,
            'top_k': top_k,
            'do_sample': do_sample,
            'repetition_penalty': repetition_penalty,
        }
        if ENABLE_CFG_SAMPLING is True:
            generate_params['guidance_scale'] = guidance_scale
            generate_params['negative_prompt_ids'] = negative_prompt_ids
            generate_params['negative_prompt_attention_mask'] = negative_prompt_attention_mask

        def generate_with_callback(callback=None, **kwargs):
            if 'stopping_criteria' in kwargs:
                kwargs['stopping_criteria'].append(Stream(callback_func=callback))
            else:
                kwargs['stopping_criteria'] = [Stream(callback_func=callback)]
            clear_torch_cache()
            with torch.no_grad():
                model.generate(**kwargs)

        def generate_with_streaming(**kwargs):
            return Iteratorize(generate_with_callback, kwargs, callback=None)

        with generate_with_streaming(**generate_params) as generator:
            for output in generator:
                next_token_ids = output[len(input_ids[0]):]
                if next_token_ids[0] == tokenizer.eos_token_id:
                    break
                new_tokens = tokenizer.decode(
                    next_token_ids, skip_special_tokens=True)
                if isinstance(tokenizer, LlamaTokenizer) and len(next_token_ids) > 0:
                    if tokenizer.convert_ids_to_tokens(int(next_token_ids[0])).startswith('▁'):
                        new_tokens = ' ' + new_tokens

                history[-1][1] = new_tokens
                yield history
                if len(next_token_ids) >= max_new_tokens:
                    break
       
def update_textbox(radio_selection):
    return radio_selection


def reset_prompt_input():
    return gr.update(value='')

def complete_loading(output_data):
    # output_data는 text_input.submit()에서 반환된 결과입니다.

    print("작업 완료:", output_data)
    # 여기에서 추가적으로 UI 업데이트나 상태 메시지 변경 등을 수행할 수 있습니다.
    return "작업이 성공적으로 완료되었습니다."

import gradio as gr

from gradio.themes.base import Base
from gradio.themes.utils.colors import Color
from gradio.themes.utils import colors, fonts, sizes
from gradio import Theme
from gradio.themes.base import Base


theme = gr.themes.Default(primary_hue="neutral").set(
    loader_color="#c4e1c7",
    slider_color="#c4e1c7",
    button_primary_background_fill="#c4e1c7",
    button_primary_background_fill_hover="#c4e1c7",
    body_background_fill="#fffef8",
    block_title_text_weight="600",
    block_border_width="3px",
    checkbox_background_color_selected="#a3bba5",
    checkbox_border_color = "neutral",
    block_label_text_weight="600",
    block_label_text_size="13px",
    checkbox_label_padding="5px",
    button_large_padding="10px",
    button_small_padding="10px",
    
)

current_option = ["다음 문장을 영어로 번역해줘", "다음 문장을 한국어로 번역해줘", "다음 메뉴를 만드는 레시피를 설명해줘", "해당 지역의 갈만한 여행지를 추천해줘"]

def option(input_text):
    global current_option  # Declare current_option as global
    current_option += [input_text]  # Add the new option
    print(current_option)
    return current_option
setup()

def reset():
    return [None]

image_path = os.path.join("/home/hslim/scripts/inference", "aai_logo.png")

import base64

# 이미지를 Base64로 인코딩
with open("/home/hslim/scripts/inference/aai_logo.png", "rb") as image_file:
    encoded_string = base64.b64encode(image_file.read()).decode()



with gr.Blocks(theme=theme) as demo:
    github_banner_path = 'https://github.com/teddysum/bllossom/blob/main/bllossom_icon.png?raw=true'
    gr.HTML(f'<p align="center"><a href="https://github.com/teddysum/bllossom"><img src="data:image/png;base64,{encoded_string}" width="200"/></a></p>')


    with gr.Column():
        with gr.Row():
            with gr.Row():
                with gr.Column():
                    radio = gr.Radio(choices = current_option, label="프롬프트")
                with gr.Column():
                    reset_button = gr.Button("Reset")

            with gr.Column(scale = 8):
                chatbot = gr.Chatbot([],
                elem_id="AAI AGAIN",
                bubble_full_width=False,
                avatar_images=(None, (os.path.join("/home/hslim/scripts/inference", "chatbot_image.png")))
                )
            with gr.Column(scale = 2):
                max_new_token = gr.Slider(
                    0,
                    4096,
                    value=512,
                    step=1.0,
                    label="Maximum New Token Length",
                    interactive=True)
                top_p = gr.Slider(0, 1, value=0.9, step=0.01,
                                label="Top P", interactive=True,elem_classes ="token_body")
                temperature = gr.Slider(
                    0,
                    1,
                    value=0.2,
                    step=0.01,
                    label="Temperature",
                    interactive=True)
                top_k = gr.Slider(1, 40, value=40, step=1,
                                label="Top K", interactive=True,elem_classes ="token_body")
                do_sample = gr.Checkbox(
                    value=True,
                    label="Do Sample",
                    info="use random sample strategy",
                    interactive=True)
                repetition_penalty = gr.Slider(
                    1.0,
                    3.0,
                    value=1.1,
                    step=0.1,
                    label="Repetition Penalty",
                    interactive=True,
                    visible=False if args.use_vllm else True)
                guidance_scale = gr.Slider(
                    1.0,
                    3.0,
                    value=1.0,
                    step=0.1,
                    label="Guidance Scale",
                    interactive=True,
                    visible=ENABLE_CFG_SAMPLING)
                presence_penalty = gr.Slider(
                    -2.0,
                    2.0,
                    value=1.0,
                    step=0.1,
                    label="Presence Penalty",
                    interactive=True,
                    visible=True if args.use_vllm else False)
                
    with gr.Column():
        with gr.Row():
            with gr.Column(scale=12):
                user_input = gr.Textbox(
                    show_label=True,
                    label="질문하세요!",
                    placeholder="Shift + Enter를 눌러 메시지 보내기...",
                    lines=5,
                    container=True)
        with gr.Row():
            with gr.Column(min_width=32, scale=1):
                submitBtn = gr.Button("입력", variant="primary")
            with gr.Column(min_width=32, scale=1):
                emptyBtn = gr.Button("Clear History")
            
        with gr.Column(scale=3):
            system_prompt_input = gr.Textbox(
                show_label=False,
                label="시스템 프롬프트",
                placeholder=DEFAULT_SYSTEM_PROMPT,
                lines=1, visible = False,
                container=False)
            negative_prompt_input = gr.Textbox(
                show_label=True,
                label="역방향 제시어(대화 시작 전 또는 과거 기록 비운 후에만 수정 유효, 대화 중 수정 무효)",
                placeholder="(선택 사항, 기본적으로 비어 있음)",
                lines=1,
                visible=False,
                container=True)


    params = [user_input, chatbot]
    predict_params = [
        chatbot,
        system_prompt_input,
        negative_prompt_input,
        max_new_token,
        top_p,
        temperature,
        top_k,
        do_sample,
        repetition_penalty,
        guidance_scale,
        presence_penalty]

    submitBtn.click(
        user,
        params,
        params,
        queue=False).then(
        predict,
        predict_params,
        chatbot).then(
            lambda: gr.update(
                interactive=True),
        None,
        [user_input],
        queue=False)


    user_input.submit(
        user,
        params,
        params,
        queue=False).then(
        predict,
        predict_params,
        chatbot).then(
            lambda: gr.update(
                interactive=True),
        None,
        [user_input],
        queue=False)

    submitBtn.click(reset_user_input, [], [user_input])

    emptyBtn.click(reset_state, outputs=[chatbot], show_progress=True)

    #radio.change(fn=update_textbox, inputs=radio, outputs=user_input)
    radio.change(fn=update_textbox, inputs=radio, outputs=system_prompt_input)

    reset_button.click(
    fn=reset,  # 클릭 시 실행할 함수
    inputs=[],  # 입력이 없음
    outputs=[radio]  # 라디오 버튼을 업데이트할 출력
    )
    
'''
    text_input.submit(fn=option, inputs=[text_input], outputs=[radio]).then(
    complete_loading,  # 추가 작업을 정의하는 함수
    outputs=[radio]) 

    pro_submitBtn.click(fn=reset_prompt_input, inputs=[text_input], outputs=[radio]).then(
    option,  # 추가 작업을 정의하는 함수    
    outputs=[radio])

    pro_submitBtn.click(
    fn=option, 
    inputs=[text_input], 
    outputs=[]
    ).then(
    lambda new_options: gr.update(choices=new_options),
    None,
    [radio])
'''
     
    #ext_input.submit(fn=option, inputs=[text_input], outputs=[radio])

    



# Launch the Gradio interface
demo.queue().launch(
    share=share,
    inbrowser=True,
    server_name='0.0.0.0',
    server_port=port)
 
