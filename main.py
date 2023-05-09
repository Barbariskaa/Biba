from EdgeGPT import Chatbot
from aiohttp import web
import time
import random
import string
import json
import re
from urllib.parse import urlparse

PORT = 8081
HOST = "127.0.0.1"

def replace_with_array(match, urls):
    index = int(match.group(1)) - 1
    return f" [{urlparse(urls[index]).hostname}]({urls[index]})"

def prepare_response(*json_objects):
    response = b""

    for obj in json_objects:
        if isinstance(obj, str):
            if obj == "DONE":
                response += b"data: " + b"[DONE]" + b"\n\n"
                continue

        response += b"data: " + json.dumps(obj).encode() + b"\n\n"

    return response


def transform_message(message):
    role = message["role"]
    content = message["content"]
    anchor = "#additional_instructions" if role == "system" else "#message"
    return f"[{role}]({anchor})\n{content}\n\n"


def process_messages(messages):
    transformed_messages = [transform_message(message) for message in messages]
    return "".join(transformed_messages)+"\n"


def response_data(id, created, content):
    return {
        "id": id,
        "created": created,
        "object": "chat.completion",
        "model": "gpt-4",
        "choices": [{
            "message": {
                "role": 'assistant',
                "content": content
            },
            'finish_reason': 'stop',
            'index': 0,
        }]
    }


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

        # Return JSON response
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

        end_data = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": "gpt-4",
            "choices": [
                {
                    "delta": {},
                    "index": 0,
                    "finish_reason": "stop"
                }
            ]
        }

        filtered_data = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": "gpt-4",
            "choices": [
                {
                    "delta": {
                        "content": "Отфильтровано."
                    },
                    "index": 0,
                    "finish_reason": "null"
                }
            ]
        }

        async def output():
            print("\nФормируется запрос...")

            non_stream_response = ""
            placeholder_wrap = ""
            placeholder_flag = False
            got_number = False
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
                                wrote = 0
                            if message.get("contentOrigin") == "Apology":
                                if stream and wrote == 0:
                                    await self.response.write(prepare_response(filtered_data))

                                if stream:
                                    await self.response.write(prepare_response(end_data, "DONE"))
                                else:
                                    await self.response.write(
                                        json.dumps(
                                            response_data(
                                                self.id,
                                                self.created,
                                                non_stream_response
                                            )
                                        ).encode()
                                    )
                                print("\nСообщение отозвано.")
                                break
                            else:
                                content = message['text'][wrote:]
                                content = content.replace('\\"', '"')
                                placeholder_number = r'\^(\d+)\^'

                                if got_number:
                                    if "]" not in content:
                                        content = placeholder_wrap + content
                                    else:
                                        content = placeholder_wrap
                                    got_number = False
                                else:
                                    if "[" in content:
                                        placeholder_flag = True

                                number_matches = re.findall(placeholder_number, content)

                                if number_matches:
                                    if placeholder_flag:
                                        placeholder_wrap = re.sub(placeholder_number,
                                                                  lambda match: replace_with_array(match, urls),
                                                                  message['text'][wrote:]
                                                                  )
                                        got_number = True
                                        placeholder_flag = False
                                    else:
                                        content = re.sub(placeholder_number,
                                                         lambda match: replace_with_array(match, urls),
                                                         message['text'][wrote:]
                                                         )
                                if not (placeholder_flag or got_number):
                                    if stream:

                                        data = {
                                            "id": self.id,
                                            "object": "chat.completion.chunk",
                                            "created": self.created,
                                            "model": "gpt-4",
                                            "choices": [
                                                {
                                                    "delta": {
                                                        "content": content
                                                    },
                                                    "index": 0,
                                                    "finish_reason": "null"
                                                }
                                            ]
                                        }

                                        await self.response.write(prepare_response(data))
                                    else:
                                        non_stream_response += content

                                print(message["text"][wrote:], end="")
                                wrote = len(message["text"])

                                if "suggestedResponses" in message:
                                    suggested_responses = '\n'.join(x["text"] for x in message["suggestedResponses"])
                                    suggested_responses = "\n```" + suggested_responses + "```"
                                    if stream:
                                        data = {
                                            "id": self.id,
                                            "object": "chat.completion.chunk",
                                            "created": self.created,
                                            "model": "gpt-4",
                                            "choices": [
                                                {
                                                    "delta": {
                                                        "content": suggested_responses
                                                    },
                                                    "index": 0,
                                                    "finish_reason": "null"
                                                }
                                            ]
                                        }
                                        if suggestion:
                                            await self.response.write(prepare_response(data, end_data, "DONE"))
                                        else:
                                            await self.response.write(prepare_response(end_data, "DONE"))
                                    else:
                                        if suggestion:
                                            non_stream_response = non_stream_response + suggested_responses
                                        await self.response.write(
                                            json.dumps(
                                                response_data(
                                                    self.id,
                                                    self.created,
                                                    non_stream_response
                                                )
                                            ).encode()
                                        )
                                    break
                if final and not response["item"]["messages"][-1].get("text"):
                    if stream:
                        await self.response.write(prepare_response(filtered_data, end_data))
                    print("Сработал фильтр.")
                    await chatbot.close()
        try:
            await output()
        except Exception as e:
            if str(e) == "'messages'":
                print("Ошибка:", str(e), "\nПроблема с учеткой. Либо забанили, либо нужно залогиниться.")
            if str(e) == " " or str(e) == "":
                print("Таймаут.")
            else:
                print("Ошибка: ", str(e))
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
