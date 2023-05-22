from EdgeGPT import Chatbot
from aiohttp import web
import time
import random
import string
import json
import re
import sys
import tiktoken
from urllib.parse import urlparse

PORT = 8081
HOST = "127.0.0.1"

DESIRED_TOKENS = 500
ASK_TO_CONTINUE_AS_A_ROLE = 'user' #user/system/assistant
CONTINUATION_QUERY = "(continue roleplay from the sentence where you have left)"
ASTERISK_FIX = True


class LinkReplacer:
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
            return self.stash
        elif self.i == 2:
            result = re.sub(r'\[\^(\d+)\^\]', lambda match: create_hyperlink(match, urls), self.stash)
            self.i = 0
            self.stash = ""
            return result


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


def create_hyperlink(match, urls):
    index = int(match.group(1)) - 1
    return f" [{urlparse(urls[index]).hostname}]({urls[index]})"


def prepare_response(id, created, filter=False, content="", end=False, done=False):

    response = b""

    if filter:
        OAIResponse = OpenaiResponse(id, created, content="Отфильтровано.")
        response += b"data: " + json.dumps(OAIResponse.dict()).encode() + b"\n\n"
    if content:
        OAIResponse = OpenaiResponse(id, created, content=content)
        response += b"data: " + json.dumps(OAIResponse.dict()).encode() + b"\n\n"
    if end:
        OAIResponse = OpenaiResponse(id, created, end=True)
        response += b"data: " + json.dumps(OAIResponse.dict()).encode() + b"\n\n"
    if done:
        response += b"data: " + b"[DONE]" + b"\n\n"

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

    id = "chatcmpl-" + ''.join(random.choices(string.ascii_letters + string.digits, k=29))
    created = str(int(time.time()))

    async def get(self):
        data = {
                   "object": "list",
                   "data": [
                       {
                        "id": "gpt-4",
                        "object": "model",
                        "created": self.created,
                        "owned_by": "OpenAI",
                        "permissions": [],
                        "root": 'gpt-4',
                        "parent": None
                       }
                   ]
               }

        return web.json_response(data)

    async def post(self):
        request_data = await self.request.json()

        messages = request_data.get('messages', [])
        prompt = messages[-1]['content']
        context = process_messages(messages[:-1])
        stream = request_data.get('stream', [])
        if stream:
            self.response = web.StreamResponse(
                status=200,
                headers={
                    'Content-Type': 'application/json',
                }
            )
            await self.response.prepare(self.request)
        else:
            self.response = web.StreamResponse(
                status=200,
                headers={
                    'Content-Type': 'application/json',
                }
            )
            await self.response.prepare(self.request)

        conversation_style = self.request.path.split('/')[1]
        if conversation_style not in ["creative", "balanced", "precise"]:
            conversation_style = "creative"

        suggestion = self.request.path.split('/')[2]
        if suggestion != "suggestion":
            suggestion = None
        try:
            chatbot = await Chatbot.create(cookie_path="cookies.json")
        except Exception as e:
            if str(e) == "[Errno 11001] getaddrinfo failed":
                print("Нет интернет соединения.")
                return
            print("Ошибка запуска чатбота.", str(e))
            return

        async def output():
            print("\nФормируется запрос...")

            link_replacer = LinkReplacer()
            response_text = ""
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
                                    await self.response.write(prepare_response(self.id, self.created, filter=True))

                                if stream:
                                    if ASTERISK_FIX and (response_text.count("*") % 2 == 1):
                                        asterisk = "*"
                                    else:
                                        asterisk = ""
                                    await self.response.write(prepare_response(self.id, self.created, content=asterisk, end=True, done=True))
                                else:
                                    if ASTERISK_FIX and len(response_text.split("*")) % 2 == 0:
                                        response_text += "*"
                                    OAIResponse = OpenaiResponse(self.id, self.created, content=response_text, stream=False)
                                    await self.response.write(
                                        json.dumps(
                                            OAIResponse.dict()
                                        ).encode()
                                    )
                                print("\nСообщение отозвано.")
                                break
                            else:
                                streamingContentChunk = message['text'][wrote:]
                                streamingContentChunk = streamingContentChunk.replace('\\"', '"')
                                response_text += streamingContentChunk

                                if 'urls' in vars():
                                    if urls:
                                        streamingContentChunk = link_replacer.process(streamingContentChunk, urls)

                                if stream:
                                    await self.response.write(prepare_response(self.id, self.created, content=streamingContentChunk))

                                print(message["text"][wrote:], end="")
                                sys.stdout.flush()
                                wrote = len(message["text"])

                                if "suggestedResponses" in message:
                                    suggested_responses = '\n'.join(x["text"] for x in message["suggestedResponses"])
                                    suggested_responses = "\n```" + suggested_responses + "```"
                                    if stream:
                                        if suggestion:
                                            await self.response.write(prepare_response(self.id, self.created, content=streamingContentChunk, end=True, done=True))
                                        else:
                                            await self.response.write(prepare_response(self.id, self.created, end=True, done=True))
                                    else:
                                        if suggestion:
                                            response_text = response_text + suggested_responses
                                        OAIResponse = OpenaiResponse(self.id, self.created, content=response_text, stream=False)
                                        await self.response.write(
                                            json.dumps(
                                                OAIResponse.dict()
                                            ).encode()
                                        )
                                    break
                if final and not response["item"]["messages"][-1].get("text"):
                    if stream:
                        await self.response.write(prepare_response(self.id, self.created, filter=True, end=True))
                    print("Сработал фильтр.")

            if response_text:
                encoding = tiktoken.get_encoding("cl100k_base")
                print(f"Всего токенов в ответе: \033[1;32m{len(encoding.encode(response_text))}\033[0m")

        try:
            await output()
            await chatbot.close()

        except Exception as e:
            error = f"Ошибка: {str(e)}."
            if str(e) == "'messages'":
                print(error, "\nПроблема с учеткой. Причины этому: \n"
                                         "  Бан. Фикс: регистрация по новой. \n"
                                         "  Куки слетели. Фикс: собрать их снова. \n"
                                         "  Достигнут лимит сообщений Бинга. Фикс: попробовать разлогиниться и собрать куки, либо собрать их с новой учетки и/или айпи."
                                         "  Возможно Бинг барахлит и нужно просто сделать реген/свайп."
                                         "Чтобы узнать подробности можно зайти в сам чат Бинга.")
            elif str(e) == " " or str(e) == "":
                print(error, "Таймаут.")
            elif str(e) == "received 1000 (OK); then sent 1000 (OK)":
                print(error, "Слишком много токенов. Больше 14000 токенов не принимает.")
            else:
                print(error)
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
          f"Также есть режим подсказок от Бинга. Чтобы его включить, нужно добавить /suggestion к концу URL, после режима.")
    web.run_app(app, host=HOST, port=PORT, print=None)