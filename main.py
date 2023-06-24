from EdgeGPT import Chatbot
from aiohttp import web
import time
import random
import string
import json
import re
import sys
import tiktoken
import config
import requests
import aiohttp
import signal
from urllib.parse import urlparse

PORT = config.PORT
HOST = config.HOST

CONCATENATE_RESPONSES = config.CONCATENATE_RESPONSES
CONCATENATE_RESPONSES_STRING = config.CONCATENATE_RESPONSES_STRING
DESIRED_TOKENS = config.DESIRED_TOKENS
CONTINUATION_QUERY = config.CONTINUATION_QUERY

MARKUP_FIX = config.MARKUP_FIX

COOKIE_NAME = config.COOKIE_NAME

USER_MESSAGE_WORKAROUND = config.USER_MESSAGE_WORKAROUND
USER_MESSAGE = config.USER_MESSAGE

REDIRECT_PROXY = config.REDIRECT_PROXY
REDIRECT_API_KEY = config.REDIRECT_API_KEY
REDIRECT_API_MODEL = config.REDIRECT_API_MODEL
REDIRECT_COMMAND = config.REDIRECT_COMMAND
REDIRECT_TEMPERATURE = config.REDIRECT_TEMPERATURE
REDIRECT_USE_CONTEXT = config.REDIRECT_USE_CONTEXT
REDIRECT_CONTEXT_TOKENS = config.REDIRECT_CONTEXT_TOKENS

try:
    cookies = json.loads(open(f"./{COOKIE_NAME}", encoding="utf-8").read())
except:
    cookies = None

class LinkPlaceholderReplacer:
    def __init__(self):
        self.placeholder_wrap = ""
        self.i = 0
        self.urls = []
        self.stash = ""
        self.regex = r'\^(\d+)\^'

    def process(self, content, urls):

        if "[" not in content and self.i == 0:
            return content

        self.stash += content

        if "[" in content:
            self.i = 1
            return ""
        elif self.i == 1 and re.search(self.regex, self.stash):
            self.i = 2
            return ""
        elif self.i == 1 and not re.search(self.regex, self.stash):
            self.i = 0
            result = self.stash
            self.stash = ""
            return result
        elif self.i == 2:
            result = re.sub(r'\[\^(\d+)\^\]', lambda match: transform_into_hyperlink(match, urls), self.stash)
            self.i = 0
            self.stash = ""
            return result
        
        self.stash = ""


class OpenaiResponse:
    def __init__(self, id, created, end=False, content="", stream=True):
        self.id = id
        self.created = created
        self.end = end
        self.content = content
        self.stream = stream

    def dict(self):
        if self.stream:
            data = {
                "id": self.id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": "gpt-4",
                "choices": [
                    {
                        "delta": {},
                        "index": 0,
                        "finish_reason": "null"
                    }
                ]
            }
            if self.end: data["choices"][0]["finish_reason"] = "stop"
            if self.content: data["choices"][0]["delta"] = {"content": self.content}
            return data
        else:
            data = {
                "id": self.id,
                "created": self.created,
                "object": "chat.completion",
                "model": "gpt-4",
                "choices": [{
                    "message": {
                        "role": 'assistant',
                        "content": self.content
                    },
                    'finish_reason': 'stop',
                    'index': 0,
                }]
            }
            return data


def transform_into_hyperlink(match, urls):
    index = int(match.group(1)) - 1
    return f" [{urlparse(urls[index]).hostname}]({urls[index]})"


def prepare_response(id, created, filter=False, content="", end=False, done=False, stream=True):

    response = b""

    if stream:
        if filter:
            OAIResponse = OpenaiResponse(id, created, content="Отфильтровано.", stream=stream)
            response += b"data: " + json.dumps(OAIResponse.dict()).encode() + b"\n\n"
        if content:
            OAIResponse = OpenaiResponse(id, created, content=content, stream=stream)
            response += b"data: " + json.dumps(OAIResponse.dict()).encode() + b"\n\n"
        if end:
            OAIResponse = OpenaiResponse(id, created, end=True, stream=stream)
            response += b"data: " + json.dumps(OAIResponse.dict()).encode() + b"\n\n"
        if done:
            response += b"data: " + b"[DONE]" + b"\n\n"
    else:
        response = json.dumps(OpenaiResponse(id, created, content=content, stream=stream).dict()).encode()

    return response


def transform_message(message):
    role = message["role"]
    content = message["content"]
    anchor = "#additional_instructions" if role == "system" else "#message"
    return f"[{role}]({anchor})\n{content}\n\n"


def process_messages(messages):
    transformed_messages = [transform_message(message) for message in messages]
    return "".join(transformed_messages)+"\n"


class SSEHandler(web.View):


    async def get(self):
        data = {
                   "object": "list",
                   "data": [
                       {
                        "id": "gpt-4",
                        "object": "model",
                        "created": str(int(time.time())),
                        "owned_by": "OpenAI",
                        "permissions": [],
                        "root": 'gpt-4',
                        "parent": None
                       }
                   ]
               }

        return web.json_response(data)

    async def post(self):

        self.id = "chatcmpl-" + ''.join(random.choices(string.ascii_letters + string.digits, k=29))
        self.created = str(int(time.time()))
        self.responseWasFiltered = False
        self.responseWasFilteredInLoop = False
        self.fullResponse = ""

        async def streamCallback(self, data):
            self.fullResponse += data
            if stream and not redirect:
                await self.response.write(b"data: " + json.dumps({
                    "id": self.id,
                    "object": "chat.completion.chunk",
                    "created": self.created,
                    "model": "gpt-4",
                    "choices": [
                        {
                            "delta": { "content": data },
                            "index": 0,
                            "finish_reason": "null"
                        }
                    ]
                }).encode() + b"\n\n")

        request_data = await self.request.json()

        messages = request_data.get('messages', [])
        if USER_MESSAGE_WORKAROUND:
            prompt = USER_MESSAGE
            context = process_messages(messages)
        else:
            prompt = messages[-1]['content']
            context = process_messages(messages[:-1])
        stream = request_data.get('stream', [])
        self.response = web.StreamResponse(
            status=200,
            headers={
                'Content-Type': 'application/json',
            }
        )
        await self.response.prepare(self.request)

        redirect = suggestion = False
        path_list = self.request.path.split('/')
        
        if path_list[1] not in ["creative", "balanced", "precise"]:
            conversation_style = "creative"
        else: conversation_style = path_list[1]

        if "suggestion" in path_list[:2]:
            suggestion = True
            
        if "redirect" in path_list[:3]:
            redirect = True

        async def output(self, streamCallback, nsfwMode=False):
            self.responseText = ""

            try:
                chatbot = await Chatbot.create(cookies=cookies)
            except Exception as e:
                bingError = str(e)
                if str(e) == "[Errno 11001] getaddrinfo failed":
                    logError = "Нет интернет-соединения: " + bingError
                if str(e) == "Authentication failed":
                    logError = "Ошибка аутентификации. Возможно стоит включить VPN: " + bingError
                print(logError)

                if stream:
                    oai_response = prepare_response(self.id, self.created, content=logError, end=True, done=True, stream=True)
                else:
                    oai_response = prepare_response(self.id, self.created, content=logError, stream=False)
                await self.response.write(oai_response)
                
                return self.response
            
            print("\nОжидание ответа от сервера...")
            link_placeholder_replacer = LinkPlaceholderReplacer()
            wrote = 0

            async for final, response in chatbot.ask_stream(
                    prompt=prompt,
                    raw=True,
                    webpage_context=context,
                    conversation_style=conversation_style,
                    search_result=True,
            ):

                if not final and response["type"] == 1 and "messages" in response["arguments"][0]:
                    message = response["arguments"][0]["messages"][0]
                    match message.get("messageType"):
                        case "InternalSearchQuery":
                            print(f"Поиск в Бинге:", message['hiddenText'])
                        case "InternalSearchResult":
                            if 'hiddenText' in message:
                                search = message['hiddenText'] = message['hiddenText'][len("```json\n"):]
                                search = search[:-len("```")]
                                search = json.loads(search)
                                urls = []
                                if "question_answering_results" in search:
                                    for result in search["question_answering_results"]:
                                        urls.append(result["url"])

                                if "web_search_results" in search:
                                    for result in search["web_search_results"]:
                                        urls.append(result["url"])
                        case None:
                            if "cursor" in response["arguments"][0]:
                                print("\nОтвет от сервера:\n")
                            if message.get("contentOrigin") == "Apology":
                                if stream and wrote == 0:
                                    await streamCallback(self, "Отфильтровано.")
                                    if nsfwMode:
                                        self.responseWasFilteredInLoop = True
                                    break

                                if MARKUP_FIX:
                                    if self.responseText.count("*") % 2 == 1 or self.responseText.count("*") == 1:
                                        await streamCallback(self, "*")
                                        self.responseText += "*"
                                    if self.responseText.count("\"") % 2 == 1 or self.responseText.count("\"") == 1:
                                        await streamCallback(self, "\"")
                                        self.responseText += "\""

                                self.responseWasFiltered = True

                                print("\nОтвет отозван во время стрима.")
                                break
                            else:
                                streaming_content_chunk = message['text'][wrote:]
                                streaming_content_chunk = streaming_content_chunk.replace('\\"', '\"')


                                if 'urls' in vars():
                                    if urls:
                                        streaming_content_chunk = link_placeholder_replacer.process(streaming_content_chunk, urls)

                                self.responseText += streaming_content_chunk

                                await streamCallback(self, streaming_content_chunk)

                                print(message["text"][wrote:], end="")
                                sys.stdout.flush()
                                wrote = len(message["text"])

                                if "suggestedResponses" in message:
                                    suggested_responses = '\n'.join(x["text"] for x in message["suggestedResponses"])
                                    suggested_responses = "\n```" + suggested_responses + "```"
                                    if suggestion and not nsfwMode:
                                        await streamCallback(self, suggested_responses)
                                    break
                if final and not response["item"]["messages"][-1].get("text"):
                    print("Сработал фильтр.")
                    if nsfwMode:
                        print("Выходим из цикла.\n")
                        self.responseWasFilteredInLoop = True
                    break

            await chatbot.close()

            
            
        try:
            if stream and not redirect:
                await self.response.write(b"data: " + json.dumps({
                    "id": self.id,
                    "object": "chat.completion.chunk",
                    "created": self.created,
                    "model": "gpt-4",
                    "choices": [
                        {
                            "delta": { "role": 'assistant' },
                            "index": 0,
                            "finish_reason": "null"
                        }
                    ]
                }).encode() + b"\n\n")
            await output(self, streamCallback)
            encoding = tiktoken.get_encoding("cl100k_base")
            if self.responseWasFiltered and CONCATENATE_RESPONSES:
                tokens_total = len(encoding.encode(self.fullResponse))
                if USER_MESSAGE_WORKAROUND:
                    prompt = CONTINUATION_QUERY
                    context += f"[assistant](#message)\n{self.responseText}\n"
                else:
                    context+=f"[{messages[-1]['role']}](#message)\n{prompt}\n\n[assistant](#message)\n{self.responseText}\n"
                    prompt=CONTINUATION_QUERY
                self.fullResponse += CONCATENATE_RESPONSES_STRING
                print("Токенов в ответе:",tokens_total)
                while tokens_total < DESIRED_TOKENS and not self.responseWasFilteredInLoop:
                    if stream and not redirect:
                        await self.response.write(b"data: " + json.dumps({
                            "id": self.id,
                            "object": "chat.completion.chunk",
                            "created": self.created,
                            "model": "gpt-4",
                            "choices": [
                                {
                                    "delta": { "content": CONCATENATE_RESPONSES_STRING },
                                    "index": 0,
                                    "finish_reason": "null"
                                }
                            ]
                        }).encode() + b"\n\n")
                    await output(self, streamCallback, nsfwMode=True)
                    context+=self.responseText + CONCATENATE_RESPONSES_STRING
                    self.fullResponse += CONCATENATE_RESPONSES_STRING
                    tokens_response = len(encoding.encode(self.responseText))
                    tokens_total = len(encoding.encode(self.fullResponse))
                    print(f"\nТокенов в ответе: {tokens_response}")
                    print(f"Токенов всего: {tokens_total}")
            if redirect:
                async with aiohttp.ClientSession() as session:
                    messages_token_count = len(encoding.encode(f"{self.fullResponse}\n\n{REDIRECT_COMMAND}"))
                    redirect_messages = [{"role": "user", "content": f"{self.fullResponse}\n\n{REDIRECT_COMMAND}"}]
                    if REDIRECT_USE_CONTEXT:
                        for message in reversed(messages):
                            if (messages_token_count + len(message["content"])) > REDIRECT_CONTEXT_TOKENS: break
                            messages_token_count += len(message["content"])
                            redirect_messages.insert(0, message)
                    headers = {"Content-Type": "application/json","Authorization": f"Bearer {REDIRECT_API_KEY}"}
                    body = {
                        "model": REDIRECT_API_MODEL,
                        "messages": redirect_messages,
                        "temperature": REDIRECT_TEMPERATURE,
                        "stream": stream
                    }
                    if REDIRECT_PROXY.endswith("v1/chat/completions") or REDIRECT_PROXY.endswith("v1/chat/completions/"):
                        url = REDIRECT_PROXY
                    elif REDIRECT_PROXY.endswith("/"):
                        url = f"{REDIRECT_PROXY}v1/chat/completions"
                    else:
                        url = f"{REDIRECT_PROXY}/v1/chat/completions"
                    async with session.post(url, headers=headers, json=body) as response:
                        async for chunk in response.content.iter_chunked(1024):
                            chunk_str = chunk.decode("utf-8")
                            if stream and not chunk_str.startswith("data: ") and chunk_str != "\n: joining queue\n\n":
                                oai_response = prepare_response(self.id, self.created, content="```\n" + chunk_str + "\n```", end=True, done=True, stream=True)
                                await self.response.write(oai_response)
                            elif not stream and not "choices" in json.loads(chunk.decode("utf-8")) and chunk.decode("utf-8") != "\n: joining queue\n\n":
                                oai_response = prepare_response(self.id, self.created, content="```\n" + chunk_str + "\n```", stream=False)
                                await self.response.write(oai_response)
                            else: await self.response.write(chunk)
            else:
                if stream:
                    await self.response.write(b"data: " + json.dumps({
                            "id": self.id, 
                            "created": self.created,
                            "object": 'chat.completion.chunk',
                            "model": "gpt-4",
                            "choices": [{
                                "delta": {},
                                "finish_reason": 'stop',
                                "index": 0,
                            }],
                        }).encode() + b"\n\n")
                else:
                    await self.response.write(json.dumps({
                            "id": self.id,
                            "created": self.created,
                            "object": "chat.completion",
                            "model": "gpt-4",
                            "choices": [{
                                "message": {
                                    "role": 'assistant',
                                    "content": self.fullResponse
                                },
                                'finish_reason': 'stop',
                                'index': 0,
                            }]
                        }).encode())
            return self.response
        except Exception as e:
            error = f"Ошибка: {str(e)}."
            error_text = ""
            if str(e) == "'messages'":
                error_text = "\nПроблема с учеткой. Возможные причины: \n```\n " \
                             "  Бан. Фикс: регистрация по новой. \n " \
                             "  Куки слетели. Фикс: собрать их снова. \n " \
                             "  Достигнут лимит сообщений Бинга. Фикс: попробовать разлогиниться и собрать куки, либо собрать их с новой учетки и/или айпи. \n " \
                             "  Возможно Бинг барахлит/троттлит запросы и нужно просто сделать реген/свайп. \n```\n " \
                             "Чтобы узнать подробности можно зайти в сам чат Бинга и отправить сообщение."
                print(error, error_text)
            elif str(e) == " " or str(e) == "":
                error_text = "Таймаут."
                print(error, error_text)
            elif str(e) == "received 1000 (OK); then sent 1000 (OK)" or str(e) == "'int' object has no attribute 'split'":
                error_text = "Слишком много токенов. Больше 14000 токенов не принимает."
                print(error, error_text)
            elif str(e) == "'contentOrigin'":
                error_text = "Ошибка связанная с размером промпта. \n " \
                             "Возможно последнее сообщение в отправленном промпте (джейл или сообщение пользователя/ассистента) " \
                             "на сервер слишком большое. \n"
                print(error, error_text)
            else:
                print(error)
            if not self.fullResponse:
                if stream:
                    oai_response = prepare_response(self.id, self.created, content=error + error_text, end=True, done=True, stream=True)
                else:
                    oai_response = prepare_response(self.id, self.created, content=error + error_text, stream=False)
            else:
                if stream:
                    oai_response = prepare_response(self.id, self.created, end=True, done=True, stream=True)
                else:
                    oai_response = prepare_response(self.id, self.created, content=self.fullResponse, stream=False)
            await self.response.write(oai_response)
        return self.response        



app = web.Application()
app.router.add_routes([
    web.route('*', '/{tail:.*}', SSEHandler),
])

if __name__ == '__main__':
    print(f"Есть несколько режимов (разнятся температурой):\n"
          f"По дефолту стоит creative: http://{HOST}:{PORT}/\n"
          f"Режим creative: http://{HOST}:{PORT}/creative\n"
          f"Режим precise:  http://{HOST}:{PORT}/precise\n"
          f"Режим balanced: http://{HOST}:{PORT}/balanced\n"
          f"Есть режим подсказок от Бинга. Чтобы его включить, нужно добавить /suggestion после выбранного режима.\n"
          f"И еще есть режим переброса, нужный для того чтобы победить шиканье креативной Сидни. Включается добавлением /redirect после режима.")
    web.run_app(app, host=HOST, port=PORT, print=None)
