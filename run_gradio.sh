CUDA_VISIBLE_DEVICES=6 python ./gradio_demo.py \
    --base_model Chang-Su/llama-2-13b-chat-ko \
    --tokenizer_path Chang-Su/llama-2-13b-chat-ko \
    --lora_model your_lora_folder \
    --port 11113
    # --max_memory 2048
    