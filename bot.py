import os
import discord
import requests
import json
import asyncio
from dotenv import load_dotenv
from collections import defaultdict
from discord import app_commands
from discord.ui import View, Button
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime
import uuid
from sqlalchemy import JSON
import logging
import time
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants
MAX_RETRIES = 3
REQUEST_TIMEOUT = 600  # 10 minutes
RATE_LIMIT_WINDOW = 60  # 1 minute
MAX_REQUESTS_PER_WINDOW = 10

# Rate limiting
request_timestamps = []

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Database setup
engine = create_engine('sqlite:///image_requests.db')
Base = declarative_base()

class ImageRequest(Base):
    __tablename__ = 'image_requests'

    id = Column(Integer, primary_key=True)
    task_id = Column(String)
    prompt = Column(String)
    user_id = Column(String)
    channel_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    discord_image_url = Column(String)
    image_urls = Column(JSON)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# Task queue
request_queue = asyncio.Queue()

# Track generation status with timeout
generation_status = {}

def init_generation_status(task_id):
    """Initialize or get status tracking for a task"""
    if task_id not in generation_status:
        generation_status[task_id] = {
            'status': 'pending',  # Start with pending instead of None
            'start_time': datetime.utcnow(),
            'retries': 0,
            'last_check': datetime.utcnow()
        }
    return generation_status[task_id]

def check_rate_limit():
    """Check if we're within rate limits"""
    current_time = time.time()
    # Remove timestamps older than the window
    while request_timestamps and request_timestamps[0] < current_time - RATE_LIMIT_WINDOW:
        request_timestamps.pop(0)
    # Check if we're at the limit
    return len(request_timestamps) < MAX_REQUESTS_PER_WINDOW

def is_request_timed_out(start_time):
    """Check if a request has timed out"""
    if not start_time:
        return False
    return (datetime.utcnow() - start_time) > timedelta(seconds=REQUEST_TIMEOUT)

async def send_image(message, image_url):
    try:
        image_response = requests.get(image_url, stream=True)
        image_response.raise_for_status()
        file = discord.File(image_response.raw, filename="image.png")
        await message.channel.send(file=file)
    except Exception as e:
        await message.channel.send(f"Could not display image: {str(e)}")

class ImageSelectView(View):
    def __init__(self, image_urls, message, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.image_urls = image_urls
        self.message = message
        for i, url in enumerate(image_urls):
            button = Button(label=f"Variation {i+1}", custom_id=f"variation_{i}")
            button.callback = self.button_callback
            self.add_item(button)

    async def button_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        index = int(interaction.custom_id.split("_")[1])
        await send_image(self.message, self.image_urls[index])

async def process_queue():
    while True:
        task_id, message = await request_queue.get()
        session = Session()
        try:
            request = session.query(ImageRequest).filter(ImageRequest.task_id == task_id).first()
            if not request:
                await asyncio.sleep(1)
                continue

            # Initialize status tracking
            status_info = init_generation_status(task_id)

            while status_info['status'] not in ['completed', 'failed']:
                # Check for timeout
                if is_request_timed_out(status_info['start_time']):
                    await message.channel.send(f"❌ Request timed out after {REQUEST_TIMEOUT/60} minutes")
                    break

                # Check rate limit
                if not check_rate_limit():
                    await asyncio.sleep(5)
                    continue

                url = f"https://api.goapi.ai/api/v1/task/{task_id}"
                headers = {
                    'x-api-key': os.getenv('GOAPI_KEY'),
                    'Content-Type': 'application/json'
                }
                
                try:
                    # Update rate limiting
                    request_timestamps.append(time.time())
                    
                    response = requests.get(url, headers=headers)
                    response.raise_for_status()
                    status_data = response.json()
                    
                    status = status_data.get('status')
                    logger.info(f"Task {task_id} - Status: {status}")
                    logger.debug(f"API Response: {json.dumps(status_data, indent=2)}")
                    
                    # Update status tracking
                    prev_status = status_info['status']
                    status_info['status'] = status or prev_status  # Keep previous status if new one is None
                    status_info['last_check'] = datetime.utcnow()
                    
                    # Debug log status transitions
                    logger.debug(f"Status transition: {prev_status} -> {status}")
                    logger.debug(f"Generation status: {status_info}")
                
                    if not status:
                        status_info['retries'] += 1
                        if status_info['retries'] >= MAX_RETRIES:
                            await message.channel.send("❌ Failed to get status from API after multiple attempts")
                            break
                        await asyncio.sleep(5)
                        continue

                    # Handle status transitions
                    if status == 'completed':
                        # Handle completion
                        output = status_data.get('output', {})
                        
                        if output:
                            # Get grid image URL
                            grid_image_url = output.get('image_url')
                            # Get individual variation URLs
                            image_urls = output.get('image_urls', [])
                            # Get Discord URL if available
                            discord_image_url = output.get('discord_image_url')
                            
                            try:
                                # First try to show the grid image
                                if grid_image_url:
                                    image_response = requests.get(grid_image_url, stream=True)
                                    image_response.raise_for_status()
                                    file = discord.File(image_response.raw, filename="grid.png")
                                    await message.channel.send(
                                        "🎨 Image generation complete!",
                                        file=file
                                    )
                                
                                # Then show variation selection if we have individual URLs
                                if image_urls:
                                    view = ImageSelectView(image_urls, message)
                                    await message.channel.send(
                                        "Select a variation to view in full size:",
                                        view=view
                                    )
                            except Exception as e:
                                logger.error(f"Error displaying image: {str(e)}")
                                await message.channel.send(
                                    f"🎨 Image generation complete, but could not display image. Error: {str(e)}"
                                )
                            
                            if not grid_image_url and not image_urls:
                                await message.channel.send("🎨 Image generation complete, but no image URLs found.")
                        else:
                            await message.channel.send("🎨 Image generation complete, but no output found.")
                        
                        request.discord_image_url = discord_image_url
                        request.image_urls = image_urls
                        session.commit()
                        break
                    elif status == 'failed':
                        await message.channel.send("❌ Image generation failed")
                        break
                    else:
                        # Log status and wait shorter time for pending/processing states
                        wait_time = 5 if status in ['pending', 'processing'] else 10
                        logging.info(f"Task ID: {task_id}, Status is {status}, waiting {wait_time} seconds...")
                        await asyncio.sleep(wait_time)
                except requests.RequestException as e:
                    logger.error(f"API request error for task {task_id}: {str(e)}")
                    generation_status[task_id]['retries'] += 1
                    if generation_status[task_id]['retries'] >= MAX_RETRIES:
                        await message.channel.send(f"❌ API request failed after {MAX_RETRIES} attempts")
                        break
                    await asyncio.sleep(5)
                    continue
                except Exception as e:
                    logger.error(f"Unexpected error for task {task_id}: {str(e)}")
                    await message.channel.send(f"❌ Unexpected error: {str(e)}")
                    break

        except Exception as e:
            logger.error(f"Error processing task {task_id}: {str(e)}")
            await message.channel.send(f"❌ Error processing request: {str(e)}")
        finally:
            session.close()
        request_queue.task_done()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    await tree.sync()
    asyncio.create_task(process_queue())

@tree.command(name="debug_task", description="Debug a specific task")
async def debug_task_command(interaction: discord.Interaction, task_id: str):
    """Debug command to check the status of a specific task"""
    await interaction.response.defer()
    
    url = f"https://api.goapi.ai/api/v1/task/{task_id}"
    headers = {
        'x-api-key': os.getenv('GOAPI_KEY'),
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        status_data = response.json()
        
        # Initialize status tracking if it doesn't exist
        status_info = init_generation_status(task_id)
        
        # Send basic status info first
        await interaction.followup.send(
            f"**Task Debug Info**\n"
            f"API Status: {status_data.get('status', 'pending')}\n"
            f"Internal Status: {status_info['status']}\n"
            f"Last Check: {status_info['last_check']}\n"
            f"Retries: {status_info['retries']}\n"
            f"Start Time: {status_info['start_time']}"
        )
        
        # Send API response in chunks
        response_json = json.dumps(status_data, indent=2)
        chunk_size = 1900
        chunks = [response_json[i:i+chunk_size] for i in range(0, len(response_json), chunk_size)]
        
        # Send first chunk with header
        await interaction.followup.send(
            f"**API Response (1/{len(chunks)})**\n```json\n{chunks[0]}\n```"
        )
        
        # Send remaining chunks
        for i, chunk in enumerate(chunks[1:], start=2):
            await interaction.followup.send(
                f"**API Response ({i}/{len(chunks)})**\n```json\n{chunk}\n```"
            )
    except Exception as e:
        await interaction.followup.send(f"❌ Error debugging task: {str(e)}")

@tree.command(name="imagine", description="Generate an image using a prompt")
async def imagine_command(interaction: discord.Interaction, prompt: str):
    # Check rate limit
    if not check_rate_limit():
        await interaction.response.send_message(
            f"⚠️ Rate limit reached. Please wait {RATE_LIMIT_WINDOW} seconds.",
            ephemeral=True
        )
        return

    await interaction.response.defer()
    
    url = "https://api.goapi.ai/api/v1/task"
    payload = json.dumps({
        "model": "midjourney",
        "task_type": "imagine",
        "input": {
            "prompt": prompt,
            "aspect_ratio": "1:1",
            "process_mode": "fast",
            "skip_prompt_check": False,
            "bot_id": 0
        },
        "config": {
            "service_mode": "",
            "webhook_config": {
                "endpoint": "",
                "secret": ""
            }
        }
    })
    headers = {
        'x-api-key': os.getenv('GOAPI_KEY'),
        'Content-Type': 'application/json'
    }

    try:
        response = requests.post(url, headers=headers, data=payload)
        response.raise_for_status()
        logging.info(f"API Response: {response.text}")
        try:
            task_id = response.json()['data']['task_id']
        except (KeyError, json.JSONDecodeError):
            await interaction.followup.send(f"❌ Error generating image: Could not extract task ID from response.")
            return
        
        session = Session()
        user_id = str(interaction.user.id)
        existing_request = session.query(ImageRequest).filter(ImageRequest.user_id == user_id).first()
        if not existing_request:
            request = ImageRequest(
                task_id=task_id,
                prompt=prompt,
                user_id=user_id,
                channel_id=str(interaction.channel.id)
            )
            session.add(request)
            session.commit()
        
        # Add task to queue
        await request_queue.put((task_id, interaction))
        session.close()
    except Exception as e:
        await interaction.followup.send(f"Error generating image: {str(e)}")

bot.run(os.getenv('DISCORD_TOKEN'))
