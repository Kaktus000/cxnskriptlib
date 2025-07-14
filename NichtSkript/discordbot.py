import discord
from discord.ext import commands, tasks
import websocket
import json
import os
import threading
import requests
import time
import asyncio
from collections import deque
import logging
import re
from datetime import datetime
import pytz

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.all()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

PANEL_URL = "DEINE_PANEL_URL"
API_KEY = os.getenv("PANEL_API_KEY")
SERVER_ID = "DEINE_SERVER_ID"

if not API_KEY:
    logging.info("Error: PANEL_API_KEY not found in secrets")
    exit(1)

neednewsocket = False
ws_connection = None
processed_messages = set()
MAX_PROCESSED_MESSAGES = 100
message_queue = deque()
is_processing_queue = False
chat_channel = None
status_channel = None
status_message = None
server_status = "OFFLINE"
player_count = 0
player_list = []
status_update_time = 0

@bot.event
async def on_ready():
    global chat_channel, status_channel
    logging.info(f'Bot is ready and logged in as {bot.user}')
    
    chat_channel_id = os.getenv("DISCORD_CHANNEL")
    if chat_channel_id and chat_channel_id.isdigit():
        chat_channel = bot.get_channel(int(chat_channel_id))
    
    status_channel_id = os.getenv("STATUS_CHANNEL", os.getenv("DISCORD_CHANNEL"))
    if status_channel_id and status_channel_id.isdigit():
        status_channel = bot.get_channel(int(status_channel_id))
        
    await create_or_update_status_message()
    update_server_status.start()
    start_websocket()

async def create_or_update_status_message():
    global status_message, status_channel, server_status, player_count, player_list, status_update_time
    
    if not status_channel:
        logging.info("Cannot create status message: Status channel not found")
        return
        
    if not status_message:
        async for msg in status_channel.history(limit=10):
            if msg.author == bot.user and "Server Status" in msg.content:
                status_message = msg
                logging.info("Found existing status message")
                break
                
    if not status_message:
        content = create_status_content()
        status_message = await status_channel.send(content)
        logging.info("Created new status message")
    else:
        await update_status_display()

def create_status_content():
    global server_status, player_count, player_list
    
    status_emoji = "🟢" if server_status == "ONLINE" else "🔴"
    status_text = "Online" if server_status == "ONLINE" else "Offline" if server_status == "OFFLINE" else "Nicht geladen"
    
    clean_player_list = []
    for player in player_list:
        clean_name = re.sub(r'\x1b\[[0-9;]*m|\[31m|\[97m|\[37m|\[0m', '' , player).strip()
        
        if "R OWNER" in clean_name:
            clean_name = clean_name.replace("R OWNER", "Owner|")
            clean_name = clean_name.replace("R? ?O?W?N?E?R", "Owner|")
            clean_name = clean_name.replace("", "Owner|")
            clean_name = clean_name.replace("?????????R? ?O?W?N?E?R" , "Owner|")
        clean_player_list.append(clean_name)
    
    if not clean_player_list:
        player_list_text = "Keine Spieler online"
    else:
        player_list_text = " -" + "\n -".join(clean_player_list).replace("?????????R? ?O?W?N?E?R" , "Owner|")

    tz = pytz.timezone('Europe/Berlin')
    timestamp = datetime.now(tz).strftime("%H:%M:%S")
    print(timestamp)
    player_count = len(clean_player_list)
    
    content = f"```\n"
    content += f"Server Status: {status_emoji} {status_text}\n"
    content += f"Spieler Online: {player_count}\n"
    content += f"Spielerliste:\n{player_list_text}\n"
    content += f"Letztes Update: {timestamp}\n"
    content += f"```"
    
    return content

@tasks.loop(seconds=10)
async def update_server_status():
    global server_status, player_count, player_list
    
    await check_server_status()
    
    if server_status == "ONLINE":
        await get_player_list()
    else:
        player_count = 0
        player_list = []
    
    await update_status_display()

async def check_server_status():
    global server_status
    
    try:
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json"
        }
        
        response = requests.get(f"{PANEL_URL}/api/client/servers/{SERVER_ID}/resources", headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            current_state = data.get('attributes', {}).get('current_state', '')
            
            if current_state == 'running':
                server_status = "ONLINE"
            else:
                server_status = "OFFLINE"
                
            logging.info(f"Server status: {server_status}")
        else:
            logging.info(f"Failed to get server status: {response.status_code} - {response.text}")
            server_status = "OFFLINE"
    except Exception as e:
        logging.info(f"Error checking server status: {e}")
        server_status = "OFFLINE"

async def get_player_list():
    global player_count, player_list
    
    try:
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        
        command_url = f"{PANEL_URL}/api/client/servers/{SERVER_ID}/command"
        payload = {"command": "list"}
        
        response = requests.post(command_url, headers=headers, json=payload)
        
        if response.status_code == 204:
            logging.info(f"Player list command sent to Minecraft")
        else:
            logging.info(f"Failed to send player list command: {response.status_code} - {response.text}")
    except Exception as e:
        logging.info(f"Error getting player list: {e}")

async def update_status_display():
    global status_message, server_status, player_count, player_list, status_update_time
    
    if not status_message or not status_channel:
        await create_or_update_status_message()
        return
        
    try:
        content = create_status_content()
        await status_message.edit(content=content)
        logging.info("Updated status message")
    except Exception as e:
        logging.info(f"Error updating status message: {e}")
        status_message = None

@bot.event
async def on_message(message):
    await bot.process_commands(message)
    
    if message.author == bot.user:
        return
    
    chat_channel_id = os.getenv("DISCORD_CHANNEL")
    if chat_channel_id and str(message.channel.id) == chat_channel_id:
        discord_name = message.author.display_name
        content = message.content
        await send_to_minecraft(discord_name, content)

async def send_to_minecraft(discord_name, content):
    try:
        content = content.replace('"', '\\"').replace("'", "\\'")
        minecraft_command = f"discordsay {discord_name} {content}"
        
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        
        command_url = f"{PANEL_URL}/api/client/servers/{SERVER_ID}/command"
        payload = {"command": minecraft_command}
        
        response = requests.post(command_url, headers=headers, json=payload)
        
        if response.status_code == 204:
            logging.info(f"Command sent to Minecraft: {minecraft_command}")
        else:
            logging.info(f"Failed to send command to Minecraft: {response.status_code} - {response.text}")
    
    except Exception as e:
        logging.info(f"Error sending command to Minecraft: {e}")

def parse_minecraft_chat(log_line):
    global player_count, player_list
    
    if 'There are ' in log_line and ' players online:' in log_line:
        try:
            count_match = re.search(r'There are (\d+)/\d+ players online', log_line)
            if count_match:
                player_count = int(count_match.group(1))
                
            if ': ' in log_line:
                players_part = log_line.split(': ')[-1].strip()
                if players_part:
                    player_list = [player.strip() for player in players_part.split(', ')]
                else:
                    player_list = []
            else:
                player_list = []
                
            logging.info(f"Updated player count: {player_count}, Player list: {player_list}")
            return None
        except Exception as e:
            logging.info(f"Error parsing player list: {e}")
    
    if '[Chat]' in log_line:
        message = log_line.split('[Chat] ')[1].strip()
        logging.info(f"Parsed chat message: {message}")
        return message
    if 'Vulcan]' in log_line:
        message = log_line.split('Vulcan] ')[1].strip() if 'Vulcan] ' in log_line else log_line
        message = "[Anticheat] " + message
        logging.info(f"Parsed Vulcan Alert: {message}")
        return message
        
    join_match = re.search(r'\[([\d:]+) INFO\]: (.*?)\[IP hidden\] logged in with entity id', log_line)
    if join_match:
        player_name = join_match.group(2)
        if player_name not in player_list:
            player_list.append(player_name)
            player_count = len(player_list)
            logging.info(f"Player joined: {player_name}, new count: {player_count}")
            bot.loop.create_task(update_status_display())
            
    leave_match = re.search(r'\[([\d:]+) INFO\]: (.*?) lost connection: (.+)', log_line)
    if leave_match:
        player_name = leave_match.group(2)
        if player_name in player_list:
            player_list.remove(player_name)
            player_count = len(player_list)
            logging.info(f"Player left: {player_name}, new count: {player_count}")
            bot.loop.create_task(update_status_display())
    
    return None

async def send_to_discord(message):
    global message_queue, is_processing_queue
    
    if message is None:
        return
    
    message_queue.append(message)
    
    if not is_processing_queue:
        is_processing_queue = True
        await process_message_queue()

async def process_message_queue():
    global message_queue, is_processing_queue, chat_channel, processed_messages
    is_processing_queue = True
    try:
        while message_queue:
            is_processing_queue = True
            message = message_queue.popleft()
            message_hash = hash(message)
            
            if message_hash in processed_messages:
                continue
                
            if chat_channel:                
                if len(processed_messages) > MAX_PROCESSED_MESSAGES:
                    processed_messages = set(list(processed_messages)[-MAX_PROCESSED_MESSAGES:])
                
                await chat_channel.send(message)
                logging.info(f"Sent to Discord: {message}")
                
                bot.loop.create_task(remove_from_processed(message_hash))
                await asyncio.sleep(0.5)
            else:
                logging.info(f"Could not find chat channel")
                
    except Exception as e:
        logging.info(f"Error sending message to Discord: {e}")
    
    is_processing_queue = False

async def remove_from_processed(message_hash):
    global processed_messages
    await asyncio.sleep(60)
    if message_hash in processed_messages:
        processed_messages.remove(message_hash)

def get_websocket_info():
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    response = requests.get(f"{PANEL_URL}/api/client/servers/{SERVER_ID}/websocket", headers=headers)
    
    if response.status_code != 200:
        logging.info(f"Error getting WebSocket info: {response.status_code} - {response.text}")
        return None, None
    
    ws_data = response.json().get('data', {})
    socket_url = ws_data.get('socket')
    token = ws_data.get('token')
    
    return socket_url, token

def on_message(ws, message):
    global ws_connection, server_status
    
    try:
        msg = json.loads(message)
        event = msg.get('event')
        args = msg.get('args', [])
        sent = False
        if event == "token expiring" or event == "token expired":
            logging.info(f"{event}, websocket wird neu gestartet...")
            ws.close()
            start_websocket()
            
        elif event == "console output" and args and len(args) > 0:
            log_line = args[0]
            logging.info("Console Outputting")
            sent = True
            if "Server started" in log_line:
                server_status = "ONLINE"
                bot.loop.create_task(update_status_display())
                
            chat_message = parse_minecraft_chat(log_line)
            if chat_message:
                logging.info(f"Minecraft Chat: {chat_message}")
                bot.loop.create_task(send_to_discord(chat_message))
                
        elif event == "status" and args and len(args) > 0:
            status = args[0]
            if status == "starting" or status == "running":
                server_status = "ONLINE"
            elif status == "stopping" or status == "offline":
                server_status = "OFFLINE"
            else:
                server_status = "NOT_LOADED"
                
            bot.loop.create_task(update_status_display())
            logging.info(f"Server status updated to: {server_status}")
        elif args and len(args) > 0:
            log_line = args[0]
            logging.info("Console Outputting")
            if "Server started" in log_line:
                server_status = "ONLINE"
                bot.loop.create_task(update_status_display())
                
            chat_message = parse_minecraft_chat(log_line)
            if chat_message:
                logging.info(f"Minecraft Chat: {chat_message}")
                if not sent:
                    bot.loop.create_task(send_to_discord(chat_message))
    except Exception as e:
        logging.info(f"Error processing WebSocket message: {e}")

def on_error(ws, error):
    logging.info(f"WebSocket error: {error}")

def on_close(ws, close_status_code, close_msg):
    logging.info(f"WebSocket connection closed: {close_status_code} - {close_msg}")

def websocket_thread():
    global ws_connection
    
    socket_url, token = get_websocket_info()
    
    if not socket_url or not token:
        logging.info("Failed to get WebSocket connection information")
        threading.Timer(30, start_websocket).start()
        return
    
    def on_open_with_token(ws):
        logging.info("WebSocket connection established")
        auth_message = json.dumps({"event": "auth", "args": [token]})
        ws.send(auth_message)
        logging.info("Authentication sent")
        ws.send(json.dumps({"event": "send logs", "args": [None]}))
    
    ws = websocket.WebSocketApp(
        socket_url,
        on_open=on_open_with_token,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    
    ws_connection = ws
    ws.run_forever()
    logging.info("WebSocket thread ended")

def start_websocket():
    global ws_connection
    
    if ws_connection:
        try:
            ws_connection.close()
        except:
            pass
    
    thread = threading.Thread(target=websocket_thread, daemon=True)
    thread.start()
    logging.info("WebSocket thread started")

@bot.command()
async def status(ctx):
    global server_status, player_count, player_list
    
    await check_server_status()
    
    if server_status == "ONLINE":
        await get_player_list()
        
    content = create_status_content()
    
    await ctx.send(content)
    
    await update_status_display()

if __name__ == "__main__":
    bot.run(os.environ['DISCORD_TOKEN'])
