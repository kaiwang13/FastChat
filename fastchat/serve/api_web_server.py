"""
A server api for chat based on the controller.
"""
import argparse
import asyncio
import dataclasses
from enum import Enum, auto
import json
import logging
import time
from typing import List, Union
import threading

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import numpy as np
import requests
import uvicorn

from fastchat.utils import build_logger, server_error_msg
from fastchat.conversation import conv_templates
from fastchat.serve.trans import detect_language, translate_to_en, translate_from_en
from fastchat.serve.gradio_web_server import get_conv_log_filename, json_dump
from asyncer import asyncify


logger = build_logger("rest_web_server", "rest_web_server.log")


def post_process_code(code):
    sep = "\n```"
    if sep in code:
        blocks = code.split(sep)
        if len(blocks) % 2 == 1:
            for i in range(1, len(blocks), 2):
                blocks[i] = blocks[i].replace("\\_", "_")
        code = sep.join(blocks)
    return code


class RESTServer:
    def __init__(self,):
        logger.info("Init REST server") 

    def conv_to_messages(self, conv, model='medgpt'):
        mapping = {
            'user': conv_templates[model].roles[0],
            'gpt': conv_templates[model].roles[1],
        }
        result = []
        for i in range(len(conv)):
            result.append([mapping[conv[i][0]], conv[i][1], conv[i][2], conv[i][3]])
        return result
    
    def messages_to_conv(self, messages, model='medgpt'):
        mapping = {
            conv_templates[model].roles[0]: 'user',
            conv_templates[model].roles[1]: 'gpt'
        }
        result = []
        for i in range(len(messages)):
            result.append([mapping[messages[i][0]], messages[i][1], messages[i][2], messages[i][3]])
        return result
    

    def generate(self, params, ip):
        start_tstamp = time.time()
        conv = conv_templates[params['model']].copy()
        conv.messages = self.conv_to_messages(params['conv'])

        text = conv.messages[-1][2]
        language = detect_language(text, args.trans_key)
        if language != 'en':
            trans_text = translate_to_en(text, language, args.trans_key)
        else:
            trans_text = text
        conv.messages[-1][1] = trans_text
        conv.messages[-1][3] = language
        conv.append_message(conv.roles[1], None, None, language)

        prompt = conv.get_prompt()
        skip_echo_len = len(prompt.replace("</s>", " ")) + 1

        params_call = {
            "model": params['model'],
            "prompt": prompt,
            "temperature": params['temperature'],
            "max_new_tokens": params['max_new_tokens'],
            "stop": "\n\n###"
        }
        try:
            response = requests.post(args.controller_url + "/worker_generate",
                json=params_call, timeout=190)
            data = json.loads(response.content.decode())
            if data["error_code"] == 0:
                output = data["text"][skip_echo_len:].strip()
                output = post_process_code(output)
                conv.messages[-1][1] = output
                if conv.messages[-1][3] != 'en':
                    conv.messages[-1][2] = translate_from_en(conv.messages[-1][1], conv.messages[-1][3], args.trans_key)
                else:
                    conv.messages[-1][2] = conv.messages[-1][1] 
            else:
                output = data["text"] + f" (error_code: {data['error_code']})"
                conv.messages[-1][1] = output
            
        except requests.exceptions.RequestException as e:
            logger.info(f"controller timeout: {args.controller_url}")
            conv.messages[-1][1] = server_error_msg

        params['conv'] = self.messages_to_conv(conv.messages)

        finish_tstamp = time.time()
        logger.info(f"{output}")

        data = {
            "tstamp": round(finish_tstamp, 4),
            "type": "chat",
            "model": params['model'],
            "comments": '',
            "start": round(start_tstamp, 4),
            "finish": round(start_tstamp, 4),
            "state": conv.dict(),
            "ip": ip,
        }
        json_dump(data, get_conv_log_filename(), mode='a')
        return params
        # print(data)

    def generate_stream(self, params, ip):
        start_tstamp = time.time()
        conv = conv_templates[params['model']].copy()
        conv.messages = self.conv_to_messages(params['conv'])

        text = conv.messages[-1][2]
        language = detect_language(text, args.trans_key)
        if language != 'en':
            trans_text = translate_to_en(text, language, args.trans_key)
        else:
            trans_text = text
        conv.messages[-1][1] = trans_text
        conv.messages[-1][3] = language
        conv.append_message(conv.roles[1], None, None, language)

        prompt = conv.get_prompt()
        skip_echo_len = len(prompt.replace("</s>", " ")) + 1

        params_call = {
            "model": params['model'],
            "prompt": prompt,
            "temperature": params['temperature'],
            "max_new_tokens": params['max_new_tokens'],
            "stop": "\n\n###"
        }
        try:
            response = requests.post(args.controller_url + "/worker_generate_stream",
                json=params_call, stream=True, timeout=20)
            for chunk in response.iter_lines(decode_unicode=False, delimiter=b"\0"):
                if chunk:
                    data = json.loads(chunk.decode())
                    if data["error_code"] == 0:
                        output = data["text"][skip_echo_len:].strip()
                        output = post_process_code(output)
                        conv.messages[-1][1] = output + "▌"
                        params['conv'] = self.messages_to_conv(conv.messages)
                        yield json.dumps(params).encode() + b"\0"
                    else:
                        output = data["text"] + f" (error_code: {data['error_code']})"
                        conv.messages[-1][1] = output
                        params['conv'] = self.messages_to_conv(conv.messages)
                        yield json.dumps(params).encode() + b"\0"
                        return
                    time.sleep(0.02)
        except requests.exceptions.RequestException as e:
            logger.info(f"controller timeout: {args.controller_url}")
            ret = {
                "text": server_error_msg,
                "error_code": 5,
            }
            yield json.dumps(params).encode() + b"\0"

        conv.messages[-1][1] = conv.messages[-1][1][:-1]
        if conv.messages[-1][3] != 'en':
            conv.messages[-1][2] = translate_from_en(conv.messages[-1][1], conv.messages[-1][3], args.trans_key)
        else:
            conv.messages[-1][2] = conv.messages[-1][1] 
        params['conv'] = self.messages_to_conv(conv.messages)
        yield json.dumps(params).encode()

        finish_tstamp = time.time()
        logger.info(f"{output}")

        data = {
            "tstamp": round(finish_tstamp, 4),
            "type": "chat",
            "model": params['model'],
            "comments": '',
            "start": round(start_tstamp, 4),
            "finish": round(start_tstamp, 4),
            "state": conv.dict(),
            "ip": ip,
        }
        # print(data)
        json_dump(data, get_conv_log_filename(), mode='a')


app = FastAPI()


@app.post("/generate_stream")
async def worker_api_generate_stream(request: Request):
    params = await request.json()
    generator = server.generate_stream(params, request.client.host)
    return StreamingResponse(generator)


@app.post("/generate")
async def worker_api_generate(request: Request):
    params = await request.json()
    result = await asyncify(server.generate)(params, request.client.host)
    return JSONResponse(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=21003)
    parser.add_argument("--controller-url", type=str, default="http://localhost:21001")
    parser.add_argument("--trans-key", required=True)
    args = parser.parse_args()
    logger.info(f"args: {args}")

    server = RESTServer()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
