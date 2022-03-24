# bot.py
import os
import random
import discord
from dotenv import load_dotenv,set_key
from discord.ext import commands
import itertools
import traceback
from async_timeout import timeout
from functools import partial
import youtube_dl 
import asyncio
from youtube_transcript_api import YouTubeTranscriptApi
from ytmusicapi import YTMusic

load_dotenv('.env')

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD = os.getenv('DISCORD_GUILD')
PREFIX = os.getenv('PREFIX')

bot = commands.Bot(command_prefix=PREFIX)

@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
	
@bot.command(name='ping')
async def ping(ctx):
    embed = discord.Embed(title='Latence', description=f'Pong! {round (bot.latency * 1000)} ms')
    await ctx.send(embed=embed)

@bot.command(name='prefix')
async def change_prefix(ctx, new_prefix: str):
    if len(new_prefix)==1:
        os.environ["PREFIX"] = new_prefix
        set_key('.env','PREFIX',os.environ["PREFIX"])
        
        bot.command_prefix = os.environ["PREFIX"]
        
        await ctx.send('Prefix changé !')
    else:
        embed = discord.Embed(title='Erreur', description=f'Le prefix {new_prefix} non valide, 1 seul caractere autorisé')
        await ctx.send(embed=embed)
        

    
ytdlopts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'flatplaylist': True,
    'cookies': 'cookies-ytdl.txt',
    'netrc': True,
    'source_address': '0.0.0.0'  # ipv6 addresses cause issues sometimes
}

ffmpegopts = {
    'before_options': '-nostdin',
    'options': '-vn'
}

ytdl = youtube_dl.YoutubeDL(ytdlopts)
ytmusic = YTMusic('auth_header.json')

class VoiceConnectionError(commands.CommandError):
    """Custom Exception class for connection errors."""


class InvalidVoiceChannel(VoiceConnectionError):
    """Exception for cases of invalid Voice Channels."""


class YTDLSource(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.
        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)
    
    @classmethod   
    def return_lyrics(cls, ctx, source):
        song = ytmusic.search(query = source.title, limit = 1, filter="songs")
        song=song[0]
        id = song.get('videoId')
        
        playlist = ytmusic.get_watch_playlist(videoId=id)
        lyric = ytmusic.get_lyrics(playlist.get('lyrics'))
        return lyric

    @classmethod
    async def playlist_entries(cls, ctx, data, player):
        source={'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}
        await player.queue.put(source)
        await ctx.send(f'```ini\n[Added {data["title"]} to the Queue.]\n```')
        
    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, download=False, fromloop, player):
        loop = loop or asyncio.get_event_loop()
        
        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        if 'entries' in data:
            for index in range(0,len(data['entries'])-1):
                await YTDLSource.playlist_entries(ctx, data['entries'][index], player)
            data = data['entries'][len(data['entries'])-1]

        if fromloop == False:
            await ctx.send(f'```ini\n[Added {data["title"]} to the Queue.]\n```')

        if download:
            source = ytdl.prepare_filename(data)
        else:
            return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

        return cls(discord.FFmpegPCMAudio(source), data=data, requester=ctx.author)


    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Used for preparing a stream, instead of downloading.
        Since Youtube Streaming links expire."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        data = await loop.run_in_executor(None, to_run)

        return cls(discord.FFmpegPCMAudio(data['url']), data=data, requester=requester)
        
     
def formate_string(string):
    string=''.join([i if ord(i) < 128 else '' for i in string])
    if string=="":
        return ""
    string=string.lower()
    string=string[0].upper()+string[1:]
    return string+'\n'
        
        


class MusicPlayer(commands.Cog):
    """A class which is assigned to each guild using the bot for Music.
    This class implements a queue and loop, which allows for different guilds to listen to different playlists
    simultaneously.
    When the bot disconnects from the Voice it's instance will be destroyed.
    """

    __slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current', 'np', 'volume')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .5
        self.current = None

        ctx.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        global loop_queue
        global loop
        """Our main player loop."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            if self.queue.empty():
                if loop == True:
                    for song in loop_queue:
                        await self.queue.put(song)

            try:
                # Wait for the next song. If we timeout cancel the player and disconnect...
                async with timeout(300):  # 5 minutes...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded)
                # So we should regather to prevent stream expiration
                try:
                    source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    await self._channel.send(f'There was an error processing your song.\n'
                                             f'```css\n[{e}]\n```')
                    continue

            source.volume = self.volume
            self.current = source

            self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            self.np = await self._channel.send(f'**Now Playing:** `{source.title}` requested by '
                                               f'`{source.requester}`')
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            source.cleanup()
            self.current = None

            try:
                # We are no longer playing this song...
                await self.np.delete()
            except discord.HTTPException:
                pass

    def destroy(self, guild):
        """Disconnect and cleanup the player."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))
        



class Music(commands.Cog):
    """Music related commands."""

    __slots__ = ('bot', 'players')
    global loop
    loop=False
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """A local check which applies to all commands in this cog."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """A local error handler for all errors arising from commands in this cog."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command can not be used in Private Messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to Voice Channel. '
                           'Please make sure you are in a valid channel or provide me with one')

        print('Ignoring exception in command {}:'.format(ctx.command), file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='connect', aliases=['join'])
    async def connect_(self, ctx):
        try:
            channel = ctx.author.voice.channel
        except AttributeError:
            raise InvalidVoiceChannel('No channel to join.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Connecting to channel: <{channel}> timed out.')
        embed = discord.Embed(title="Channel connection")
        embed.add_field(name="Connecté a :", value=channel, inline=True)

        await ctx.send(embed=embed)

    @commands.command(name='play', aliases=['sing', 'p'])
    async def play_(self, ctx, *, search: str):
        await ctx.trigger_typing()
        global loop
        global loop_queue
        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)

        
            
        # If download is False, source will be a dict which will be used later to regather the stream.
        # If download is True, source will be a discord.FFmpegPCMAudio with a VolumeTransformer.
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, download=False, fromloop=False, player=player)

        await player.queue.put(source)
        if loop == True:
            listtitle = list(itertools.islice(player.queue._queue, 0, None))
            a=0
            loop_queue=[]
            for song in listtitle:
                loop_queue.append(await YTDLSource.create_source(ctx, song, loop=self.bot.loop, download=False, fromloop=True, player=player))
            loop_queue.insert(0,await YTDLSource.create_source(ctx, vc.source.title, loop=self.bot.loop, download=False, fromloop=True, player=player))
            
    @commands.command(name='pause')
    async def pause_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('I am not currently playing anything!')
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send(f'**`{ctx.author}`**: Paused the song!')
        

     
    @commands.command(name='reset')
    async def reset_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('I am not currently playing anything!')
        
        player = self.get_player(ctx)
        song = vc.current
        
        temp_song=[]
        interval=player.queue.qsize()
        for i in range (interval):
            temp_song.append(await player.queue.get())
        temp_song.insert(0, song)
        
        for i in range (interval+1):
            await player.queue.put(temp_song[i])
            
        await ctx.send(f'**`{ctx.author}`**: The song has been reset!')

     
     
    @commands.command(name='lyrics', aliases=['lyric', 'ly'])
    async def lyrics_(self, ctx):
        """Retrieve the current video lyrics"""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('I am not currently playing anything!')
        video_id = vc.source.web_url[32:]
        """
        lyrics = YouTubeTranscriptApi.get_transcript(video_id)
        formated_lyric = ""
        for ligne in lyrics:
            if '[' in ligne.get('text'):
                pass
            elif len(formated_lyric+ligne.get('text')) >= 1024:
            
                embed = discord.Embed(title="Lyrics of "+vc.source.title)
                embed.add_field(name="lyrics : ", value=formated_lyric, inline=False)
                await ctx.send(embed=embed)
                formated_lyric=formate_string(ligne.get('text'))

            else:
            
                formated_lyric=formated_lyric+formate_string(ligne.get('text'))
                
        embed = discord.Embed(title="Lyrics of "+vc.source.title)
        embed.add_field(name="lyrics : ", value=formated_lyric, inline=False)
        await ctx.send(embed=embed)
        """
        lyrics = YTDLSource.return_lyrics(ctx, vc.source)

        formated_lyric = ""
        if lyrics == None:
            lyrics = YouTubeTranscriptApi.get_transcript(video_id)
            
            for ligne in lyrics:
                if '[' in ligne.get('text'):
                    pass
                elif len(formated_lyric+ligne.get('text')) >= 1024:
                
                    embed = discord.Embed(title="Lyrics of "+vc.source.title)
                    embed.add_field(name="lyrics : ", value=formated_lyric, inline=False)
                    await ctx.send(embed=embed)
                    formated_lyric=formate_string(ligne.get('text'))

                else:
                
                    formated_lyric=formated_lyric+formate_string(ligne.get('text'))
                    
            embed = discord.Embed(title="Lyrics of "+vc.source.title)
            embed.add_field(name="lyrics : ", value=formated_lyric, inline=False)
            embed.add_field(name="Source : ", value="Automately generated lyrics", inline=True)
            await ctx.send(embed=embed)
            
        else:
            list_ly = lyrics.get('lyrics').split('\n')
            
            for ligne in list_ly:
                if len(formated_lyric+ligne) >= 1024:
                
                    embed = discord.Embed(title="Lyrics of "+vc.source.title)
                    embed.add_field(name="lyrics : ", value=formated_lyric, inline=False)
                    await ctx.send(embed=embed)
                    formated_lyric=ligne+'\n'

                else:
                
                    formated_lyric=formated_lyric+ligne+'\n'
                    
            embed = discord.Embed(title="Lyrics of "+vc.source.title)
            embed.add_field(name="lyrics : ", value=formated_lyric, inline=False)
            embed.add_field(name="Source : ", value=lyrics.get('source'), inline=True)
            await ctx.send(embed=embed)
        
    @commands.command(name='remove', aliases=['rm'])
    async def remove_(self, ctx, index: int):
        """remove the ieme song."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('I am not currently playing anything!')

        player = self.get_player(ctx)
        temp_song=[]
        interval=player.queue.qsize()
        for i in range (interval):
            temp_song.append(await player.queue.get())
        if index<=0 or index>interval:
            return await ctx.send('Uncorrect song number')

        del temp_song[index-1]
        for i in temp_song:
            await player.queue.put(i)
        await ctx.send(f'**`{ctx.author}`**: Removed a song!')

    @commands.command(name='resume', aliases=['unpause'])
    async def resume_(self, ctx):
        """Resume the currently paused song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!', )
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send(f'**`{ctx.author}`**: Resumed the song!')

    @commands.command(name='skip', aliases=['fs'])
    async def skip_(self, ctx):
        """Skip the song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!')

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()
        await ctx.send(f'**`{ctx.author}`**: Skipped the song!')

    @commands.command(name='queue', aliases=['q', 'playlist'])
    async def queue_info(self, ctx):
        """Retrieve a basic queue of upcoming songs."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!')

        player = self.get_player(ctx)
        if player.queue.empty():
            return await ctx.send('There are currently no more queued songs.')

        # Grab up to 5 entries from the queue...
        upcoming = list(itertools.islice(player.queue._queue, 0, 5))

        fmt = '\n'.join(f'**`{_["title"]}`**' for _ in upcoming)
        embed = discord.Embed(title=f'Upcoming - Next {len(upcoming)}', description=fmt)

        await ctx.send(embed=embed)

    @commands.command(name='loop', aliases=['lp'])
    async def loop_queue(self, ctx):
        vc = ctx.voice_client
        global loop
        global last
        global loop_queue
        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!')

        player = self.get_player(ctx)
        
        if loop == False:
            loop=True
            await ctx.send(f'**`{ctx.author}`**: Looped the queue!')
            listtitle = list(itertools.islice(player.queue._queue, 0, None))
            a=0
            loop_queue=[]
            for song in listtitle:
                loop_queue.append(await YTDLSource.create_source(ctx, song, loop=self.bot.loop, download=False, fromloop=True, player=player))
            loop_queue.insert(0,await YTDLSource.create_source(ctx, vc.source.title, loop=self.bot.loop, download=False, fromloop=True, player=player))
        else:
            loop=False
            await ctx.send(f'**`{ctx.author}`**: Unlooped the queue!')
        
        
    @commands.command(name='now_playing', aliases=['np', 'current', 'currentsong', 'playing'])
    async def now_playing_(self, ctx):
        """Display information about the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!', )

        player = self.get_player(ctx)
        if not player.current:
            return await ctx.send('I am not currently playing anything!')

        try:
            # Remove our previous now_playing message.
            await player.np.delete()
        except discord.HTTPException:
            pass

        player.np = await ctx.send(f'**Now Playing:** `{vc.source.title}` '
                                   f'requested by `{vc.source.requester}`')

    @commands.command(name='volume', aliases=['vol'])
    async def change_volume(self, ctx, *, vol: float):
        """Change the player volume.
        Parameters
        ------------
        volume: float or int [Required]
            The volume to set the player to in percentage. This must be between 1 and 100.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!', )

        if not 0 < vol < 101:
            return await ctx.send('Please enter a value between 1 and 100.')

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume = vol / 100

        player.volume = vol / 100
        embed = discord.Embed(title="Volume Message",
        description=f'The Volume Was Changed By **{ctx.author.name}**')
        embed.add_field(name="Current Volume", value=vol, inline=True)
        await ctx.send(embed=embed)
        # await ctx.send(f'**`{ctx.author}`**: Set the volume to **{vol}%**')

    @commands.command(name='stop', aliases=['leave','shutup'])
    async def stop_(self, ctx):
        """Stop the currently playing song and destroy the player.
        !Warning!
            This will destroy the player assigned to your guild, also deleting any queued songs and settings.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!')

        await self.cleanup(ctx.guild)
        
bot.add_cog(Music(bot))
bot.run(TOKEN)
