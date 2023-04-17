import argparse
from collections import defaultdict
import datetime
import json
import os
import time
import uuid
import requests
import gradio as gr

from fastchat.conversation import (default_conversation, conv_templates,
                                   SeparatorStyle)

from fastchat.constants import LOGDIR
from fastchat.utils import (build_logger, server_error_msg,
    violates_moderation, moderation_msg)
from fastchat.serve.gradio_patch import Chatbot as grChatbot
from fastchat.serve.gradio_css import code_highlight_css
from fastchat.serve.trans import detect_language, translate_to_en, translate_from_en

logger = build_logger("gradio_web_server", "gradio_web_server.log")

headers = {"User-Agent": "fastchat Client"}

no_change_btn = gr.Button.update()
enable_btn = gr.Button.update(interactive=True)
enable_tb = gr.Textbox.update(interactive=True)
disable_clear_tb = gr.Textbox.update(value='', interactive=False)
disable_btn = gr.Button.update(interactive=False)


def json_dump(obj, path, mode='a'):
    with open(path, mode, encoding='utf8') as f:
        json.dump(obj, f, ensure_ascii=False, sort_keys=True)
        f.write('\n')


priority = {
    "vicuna-13b": "aaaaaaa",
    "koala-13b": "aaaaaab",
}

def get_conv_log_filename():
    t = datetime.datetime.now()
    name = os.path.join(LOGDIR, f"{t.year}-{t.month:02d}-{t.day:02d}-conv.json")
    return name


def get_model_list():
    ret = requests.post(args.controller_url + "/refresh_all_workers")
    assert ret.status_code == 200
    ret = requests.post(args.controller_url + "/list_models")
    models = ret.json()["models"]
    models.sort(key=lambda x: priority.get(x, x))
    logger.info(f"Models: {models}")
    return models


get_window_url_params = """
function() {
    const params = new URLSearchParams(window.location.search);
    url_params = Object.fromEntries(params);
    console.log(url_params);
    return url_params;
    }
"""


def load_demo(url_params, request: gr.Request):
    logger.info(f"load_demo. ip: {request.client.host}. params: {url_params}")

    dropdown_update = gr.Dropdown.update(visible=True)
    if "model" in url_params:
        model = url_params["model"]
        if model in models:
            dropdown_update = gr.Dropdown.update(
                value=model, visible=True)

    state = default_conversation.copy()
    return (state,
            dropdown_update,
            gr.Chatbot.update(visible=True),
            gr.Textbox.update(visible=True),
            gr.Textbox.update(visible=True),
            gr.Button.update(visible=True),
            gr.Row.update(visible=True),
            gr.Accordion.update(visible=True))


def load_demo_refresh_model_list(request: gr.Request):
    logger.info(f"load_demo. ip: {request.client.host}")
    models = get_model_list()
    state = default_conversation.copy()
    return (state, gr.Dropdown.update(
               choices=models,
               value=models[0] if len(models) > 0 else ""),
            gr.Chatbot.update(visible=True),
            gr.Textbox.update(visible=True),
            gr.Textbox.update(visible=True),
            gr.Button.update(visible=True),
            gr.Row.update(visible=True),
            gr.Accordion.update(visible=True))


def vote_last_response(state, vote_type, model_selector, comments, request: gr.Request):
    data = {
        "tstamp": round(time.time(), 4),
        "type": vote_type,
        "model": model_selector,
        "comments": comments,
        "state": state.dict(),
        "ip": request.client.host,
    }
    json_dump(data, get_conv_log_filename(), mode='a')


def upvote_last_response(state, model_selector, comments, request: gr.Request):
    logger.info(f"upvote. ip: {request.client.host}")
    vote_last_response(state, "upvote", model_selector, comments, request)
    return ("",disable_clear_tb) + (disable_btn,) * 3


def downvote_last_response(state, model_selector, comments, request: gr.Request):
    logger.info(f"downvote. ip: {request.client.host}")
    vote_last_response(state, "downvote", model_selector, comments, request)
    return ("",disable_clear_tb) + (disable_btn,) * 3


def flag_last_response(state, model_selector, comments, request: gr.Request):
    logger.info(f"flag. ip: {request.client.host}")
    vote_last_response(state, "flag", model_selector, comments, request)
    return ("",disable_clear_tb) + (disable_btn,) * 3


def regenerate(state, request: gr.Request):
    logger.info(f"regenerate. ip: {request.client.host}")
    state.messages[-1][1] = None
    state.skip_next = False
    return (state, state.to_gradio_chatbot(), "") + (disable_btn,) * 4


def clear_history(request: gr.Request):
    logger.info(f"clear_history. ip: {request.client.host}")
    state = default_conversation.copy()
    return (state, state.to_gradio_chatbot(), "") + (disable_btn,) * 4


def add_text(state, text, request: gr.Request):
    language = detect_language(text)
    logger.info(f"add_text. ip: {request.client.host}. len: {len(text)}")
    if len(text) <= 0:
        state.skip_next = True
        return (state, state.to_gradio_chatbot(), "") + (no_change_btn,) * 4
    if args.moderate:
        flagged = violates_moderation(text)
        if flagged:
            state.skip_next = True
            return (state, state.to_gradio_chatbot(), moderation_msg) + (
                no_change_btn,) * 4
    if language != 'en':
        trans_text = translate_to_en(text, language)
    else:
        trans_text = text
    text = text[:5000]  # Hard cut-off
    state.append_message(state.roles[0], trans_text, text, language)
    state.append_message(state.roles[1], None, None, language)
    state.skip_next = False
    return (state, state.to_gradio_chatbot(), "") + (disable_btn,) * 4


def post_process_code(code):
    sep = "\n```"
    if sep in code:
        blocks = code.split(sep)
        if len(blocks) % 2 == 1:
            for i in range(1, len(blocks), 2):
                blocks[i] = blocks[i].replace("\\_", "_")
        code = sep.join(blocks)
    return code


def http_bot(state, model_selector, temperature, max_new_tokens, request: gr.Request):
    logger.info(f"http_bot. ip: {request.client.host}")
    start_tstamp = time.time()
    model_name = model_selector

    if state.skip_next:
        # This generate call is skipped due to invalid inputs
        yield (state, state.to_gradio_chatbot()) + (no_change_btn,) * 4
        return

    if len(state.messages) == state.offset + 2:
        # First round of conversation
        if "koala" in model_name: # Hardcode the condition
            template_name = "bair_v1"
        elif 'medgpt' in model_name:
            template_name = "medgpt"
        else:
            template_name = "v1"
        new_state = conv_templates[template_name].copy()
        new_state.conv_id = uuid.uuid4().hex
        new_state.append_message(new_state.roles[0], state.messages[-2][1], state.messages[-2][2], state.messages[-2][3])
        new_state.append_message(new_state.roles[1], state.messages[-1][1], state.messages[-1][2], state.messages[-1][3])
        state = new_state

    # Query worker address
    controller_url = args.controller_url
    ret = requests.post(controller_url + "/get_worker_address",
            json={"model": model_name})
    worker_addr = ret.json()["address"]
    logger.info(f"model_name: {model_name}, worker_addr: {worker_addr}")

    # No available worker
    if worker_addr == "":
        state.messages[-1][1] = server_error_msg
        yield (state, state.to_gradio_chatbot(), disable_clear_tb, disable_btn, disable_btn, disable_btn, enable_btn)
        return

    # Construct prompt
    if "chatglm" in model_name:
        prompt = state.messages[state.offset:]
        skip_echo_len = len(state.messages[-2][1]) + 1
    else:
        prompt = state.get_prompt()
        skip_echo_len = len(prompt.replace("</s>", " ")) + 1

    # Make requests
    pload = {
        "model": model_name,
        "prompt": prompt,
        "temperature": float(temperature),
        "max_new_tokens": int(max_new_tokens),
        "stop": state.sep if state.sep_style == SeparatorStyle.SINGLE else state.sep2,
    }
    logger.info(f"==== request ====\n{pload}")

    state.messages[-1][1] = "▌"
    yield (state, state.to_gradio_chatbot(), enable_tb) + (disable_btn,) * 4

    try:
        # Stream output
        response = requests.post(worker_addr + "/worker_generate_stream",
            headers=headers, json=pload, stream=True, timeout=20)
        for chunk in response.iter_lines(decode_unicode=False, delimiter=b"\0"):
            if chunk:
                data = json.loads(chunk.decode())
                if data["error_code"] == 0:
                    output = data["text"][skip_echo_len:].strip()
                    output = post_process_code(output)
                    state.messages[-1][1] = output + "▌"
                    yield (state, state.to_gradio_chatbot(), enable_tb) + (disable_btn,) * 4
                else:
                    output = data["text"] + f" (error_code: {data['error_code']})"
                    state.messages[-1][1] = output
                    yield (state, state.to_gradio_chatbot(), enable_tb) + (disable_btn, disable_btn, disable_btn, enable_btn)
                    return
                time.sleep(0.02)
    except requests.exceptions.RequestException as e:
        state.messages[-1][1] = server_error_msg + f" (error_code: 4)"
        yield (state, state.to_gradio_chatbot(), enable_tb) + (disable_btn, disable_btn, disable_btn, enable_btn)
        return


    state.messages[-1][1] = state.messages[-1][1][:-1]
    if state.messages[-1][3] != 'en':
        state.messages[-1][2] = translate_from_en(state.messages[-1][1], state.messages[-1][3])
    yield (state, state.to_gradio_chatbot(), enable_tb) + (enable_btn,) * 4

    finish_tstamp = time.time()
    logger.info(f"{output}")

    data = {
        "tstamp": round(finish_tstamp, 4),
        "type": "chat",
        "model": model_name,
        "comments": '',
        "start": round(start_tstamp, 4),
        "finish": round(start_tstamp, 4),
        "state": state.dict(),
        "ip": request.client.host,
    }
    json_dump(data, get_conv_log_filename(), mode='a')


notice_markdown = ("""
# 👨‍⚕️🩺 Chat with Medical Large Language Model with Multilingual Supports.
### Usage
**You can ask questions about:**
- **explanations of medical terms**
- **differential diagnoses**
- **medical advice**
- **other questions you want to ask**
**The response will be translated according to the language of your question.**

### Terms of use
By using this service, users are required to agree to the following terms: The service is a research preview intended for non-commercial use only. It only provides limited safety measures and may generate offensive content. It must not be used for any illegal, harmful, violent, racist, or sexual purposes. The service may collect user dialogue data for future research.
""")


learn_more_markdown = ("""
### License
The service is a research preview intended for non-commercial use only, subject to the model [License](https://github.com/facebookresearch/llama/blob/main/MODEL_CARD.md) of LLaMA. Please contact us if you find any potential violation.
""")


css = code_highlight_css + """
pre {
    white-space: pre-wrap;       /* Since CSS 2.1 */
    white-space: -moz-pre-wrap;  /* Mozilla, since 1999 */
    white-space: -pre-wrap;      /* Opera 4-6 */
    white-space: -o-pre-wrap;    /* Opera 7 */
    word-wrap: break-word;       /* Internet Explorer 5.5+ */
}
"""


def build_demo():
    with gr.Blocks(title="FastChat", theme=gr.themes.Base(), css=css) as demo:
        state = gr.State()

        # Draw layout
        notice = gr.Markdown(notice_markdown)

        with gr.Row(elem_id="model_selector_row"):
            model_selector = gr.Dropdown(
                choices=models,
                value=models[0] if len(models) > 0 else "",
                interactive=True,
                show_label=False).style(container=False)

        chatbot = grChatbot(elem_id="chatbot", visible=False).style(height=550)
        with gr.Row():
            with gr.Column(scale=20):
                textbox = gr.Textbox(show_label=False,
                    placeholder="Enter text and press ENTER", visible=False).style(container=False)
            with gr.Column(scale=1, min_width=50):
                submit_btn = gr.Button(value="Send", visible=False)
        with gr.Row():
            with gr.Column():
                commentbox = gr.Textbox(show_label=False,
                    placeholder="Enter comment and press Upvote, Downvote, or Flag", visible=False).style(container=False)

        with gr.Row(visible=False) as button_row:
            upvote_btn = gr.Button(value="👍  Upvote", interactive=False)
            downvote_btn = gr.Button(value="👎  Downvote", interactive=False)
            flag_btn = gr.Button(value="⚠️  Flag", interactive=False)
            #stop_btn = gr.Button(value="⏹️  Stop Generation", interactive=False)
            # regenerate_btn = gr.Button(value="🔄  Regenerate", interactive=False)
            clear_btn = gr.Button(value="🗑️  Clear history", interactive=False)

        with gr.Accordion("Parameters", open=False, visible=False) as parameter_row:
            temperature = gr.Slider(minimum=0.0, maximum=1.0, value=0.7, step=0.1, interactive=True, label="Temperature",)
            max_output_tokens = gr.Slider(minimum=0, maximum=1024, value=512, step=64, interactive=True, label="Max output tokens",)

        # gr.Markdown(learn_more_markdown)
        url_params = gr.JSON(visible=False)

        # Register listeners
        btn_list = [upvote_btn, downvote_btn, flag_btn, clear_btn]
        upvote_btn.click(upvote_last_response,
            [state, model_selector, commentbox], [textbox, commentbox, upvote_btn, downvote_btn, flag_btn])
        downvote_btn.click(downvote_last_response,
            [state, model_selector, commentbox], [textbox, commentbox, upvote_btn, downvote_btn, flag_btn])
        flag_btn.click(flag_last_response,
            [state, model_selector, commentbox], [textbox, commentbox, upvote_btn, downvote_btn, flag_btn])
        # regenerate_btn.click(regenerate, state,
        #     [state, chatbot, textbox] + btn_list).then(
        #     http_bot, [state, model_selector, temperature, max_output_tokens],
        #     [state, chatbot] + btn_list)
        clear_btn.click(clear_history, None, [state, chatbot, textbox] + btn_list)

        textbox.submit(add_text, [state, textbox], [state, chatbot, textbox] + btn_list
            ).then(http_bot, [state, model_selector, temperature, max_output_tokens],
                   [state, chatbot, commentbox] + btn_list)
        submit_btn.click(add_text, [state, textbox], [state, chatbot, textbox] + btn_list
            ).then(http_bot, [state, model_selector, temperature, max_output_tokens],
                   [state, chatbot, commentbox] + btn_list)

        if args.model_list_mode == "once":
            demo.load(load_demo, [url_params], [state, model_selector,
                chatbot, textbox, commentbox, submit_btn, button_row, parameter_row],
                _js=get_window_url_params)
        elif args.model_list_mode == "reload":
            demo.load(load_demo_refresh_model_list, None, [state, model_selector,
                chatbot, textbox, commentbox, submit_btn, button_row, parameter_row])
        else:
            raise ValueError(f"Unknown model list mode: {args.model_list_mode}")

    return demo


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int)
    parser.add_argument("--controller-url", type=str, default="http://localhost:21001")
    parser.add_argument("--concurrency-count", type=int, default=10)
    parser.add_argument("--model-list-mode", type=str, default="once",
        choices=["once", "reload"])
    parser.add_argument("--share", action="store_true")
    parser.add_argument("--moderate", action="store_true",
        help="Enable content moderation")
    args = parser.parse_args()
    logger.info(f"args: {args}")

    models = get_model_list()

    logger.info(args)
    demo = build_demo()
    demo.queue(concurrency_count=args.concurrency_count, status_update_rate=10,
               api_open=False).launch(server_name=args.host, server_port=args.port,
                                      share=args.share, max_threads=200)
