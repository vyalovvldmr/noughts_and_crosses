import asyncio
import uuid
from random import randint
from contextlib import suppress
from enum import IntEnum
import logging

import click
import aiohttp
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionError
from rich.panel import Panel
from rich.console import Console, ConsoleOptions, RenderResult, RenderableType
from rich.text import Text
from rich.align import Align
from rich.table import Table
from pyfiglet import Figlet
from textual.app import App
from textual.views import GridView
from textual.widget import Widget
from textual.widgets import Footer as _Footer, Header as _Header
from textual.reactive import Reactive
from textual import events

from ttt.version import VERSION
from ttt import settings
from ttt.app import get_application
from ttt.game import BoxType, GameStatus
from ttt.ws_utils import WsEvent, WsGameStateEvent, WsOperation, WsOperationPayload


class WebsocketConnectionState(IntEnum):
    CONNECTED = 1
    DISCONNECTED = 2


class Connect(events.Event):
    pass


class Disconnect(events.Event):
    pass


class Header(_Header):
    def render(self) -> RenderableType:
        header_table = Table.grid(padding=(0, 1), expand=True)
        header_table.style = self.style
        header_table.add_column("title", justify="center", ratio=1)
        header_table.add_column("clock", justify="right", width=8)
        header_table.add_row(self.full_title, self.get_clock() if self.clock else "")
        header: RenderableType
        header = Panel(header_table, style=self.style) if self.tall else header_table
        return header


class Footer(_Footer):
    def __init__(self) -> None:
        super().__init__()
        self._connection_text: str = "Disconnected"
        self._connection_style: str = "white on dark_red"

    def render(self) -> RenderableType:
        if self._key_text is None:
            self._key_text = self.make_key_text()
            self._key_text.append_text(Text(f"| Websocket: {self._connection_text}"))
            self._key_text.style = self._connection_style
        return self._key_text

    def on_connect(self):
        self._key_text = None
        self._connection_text = "Connected"
        self._connection_style = "white on dark_green"
        self.refresh()

    def on_disconnect(self):
        self._key_text = None
        self._connection_text = "Disconnected"
        self._connection_style = "white on dark_red"
        self.refresh()


class FigletText:
    def __init__(self, text: str) -> None:
        self.text = text

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        size = min(options.max_width / 2, options.max_height)
        if size < 4:
            yield Text(self.text, style="bold")
        else:
            if size < 7:
                font_name = "mini"
            elif size < 8:
                font_name = "small"
            elif size < 10:
                font_name = "standard"
            else:
                font_name = "big"
            font = Figlet(font=font_name, width=options.max_width)
            yield Text(font.renderText(self.text).rstrip("\n"), style="bold")


class Tile(Widget):
    mouse_over = Reactive(False)

    def __init__(self, name: str | None = None, num: int | None = None) -> None:
        super().__init__(name)
        self._text: str = ""
        self._num = num

    def render(self) -> Panel:
        return Panel(
            Align.center(FigletText(self._text), vertical="middle"),
            style=("on red" if self.mouse_over else ""),
        )

    async def on_enter(self) -> None:
        self.mouse_over = True

    async def on_leave(self) -> None:
        self.mouse_over = False

    async def on_click(self, event: events.Click) -> None:
        await self.app.make_turn(self._num)


class Grid(GridView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tiles: tuple[Tile] = tuple(
            Tile(num=i) for i in range(settings.GRID_SIZE**2)
        )

    async def on_mount(self, event: events.Mount) -> None:
        self.grid.set_gap(1, 0)
        self.grid.set_gutter(1)
        self.grid.set_align("center", "center")

        self.grid.add_column("col", min_size=5, max_size=30, repeat=3)
        self.grid.add_row("row", min_size=5, max_size=30, repeat=3)

        self.grid.place(*self.tiles)


class GameApp(App):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._footer: Footer = Footer()
        self._grid: Grid = Grid()
        self._player_id = str(uuid.uuid4())
        self._ws: None | web.WebSocketResponse = None
        self.game_status: int = GameStatus.awaiting
        self.whose_turn: str = ""
        self._box_types = {
            BoxType.empty: " ",
            BoxType.nought: "0",
            BoxType.cross: "X",
        }
        self._websocket_connection_state: WebsocketConnectionState = (
            WebsocketConnectionState.DISCONNECTED
        )

    async def on_mount(self) -> None:
        await self.view.dock(Header(style="", clock=False), edge="top")
        await self.view.dock(self._footer, edge="bottom")
        await self.view.dock(self._grid)

    async def on_load(self) -> None:
        asyncio.ensure_future(self.keep_connection())
        await self.bind("q", "quit", "Quit")

    async def keep_connection(self):
        URL = "ws://{host}:{port}/ws".format(
            host=settings.SERVER_IP, port=settings.SERVER_PORT
        )
        while True:
            with suppress(ClientConnectionError):
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        URL,
                        headers={
                            "Cookie": "player_id={player_id}".format(
                                player_id=self._player_id
                            )
                        },
                    ) as ws:
                        if (
                            self._websocket_connection_state
                            == WebsocketConnectionState.DISCONNECTED
                        ):
                            self._websocket_connection_state = (
                                WebsocketConnectionState.CONNECTED
                            )
                            self._footer.post_message_no_wait(Connect(self))
                        self._ws = ws
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                ws_event = WsEvent.parse_raw(msg.data)
                                await self.on_ws_event(ws_event)
            if self._websocket_connection_state == WebsocketConnectionState.CONNECTED:
                self._websocket_connection_state = WebsocketConnectionState.DISCONNECTED
                self._footer.post_message_no_wait(Disconnect(self))
            await asyncio.sleep(settings.CLIENT_RECONNECT_TIMEOUT)

    async def on_ws_event(self, event: WsEvent) -> None:
        if isinstance(event.data, WsGameStateEvent):
            self.game_status = event.data.payload.status
            self.whose_turn = event.data.payload.whose_turn
            for num, box_type in enumerate(event.data.payload.grid):
                self._grid.tiles[num]._text = self._box_types[box_type]
                self._grid.tiles[num].refresh()

    async def make_turn(self, tile_num: int) -> None:
        if (
            self.game_status == GameStatus.in_progress
            and self.whose_turn == self._player_id
        ):
            try:
                await self._ws.send_json(
                    WsOperation(payload=WsOperationPayload(turn=tile_num)).dict()
                )
            except ConnectionResetError as err:
                self.log(err)


async def run_server() -> web.Application:
    app = get_application()

    await asyncio.get_event_loop().create_server(
        app.make_handler(), settings.SERVER_IP, settings.SERVER_PORT
    )

    logging.info(
        "server started at ws://%s:%s", settings.SERVER_IP, settings.SERVER_PORT
    )

    return app


async def shutdown_server(app: web.Application) -> None:
    for ws in app["websockets"]:
        await ws.close()


@click.command()
@click.option("-d", "--daemon", is_flag=True, help="Run server.")
def main(daemon):
    """
    Noughts & Crosses game. Client and server command.
    """
    if daemon:
        logging.getLogger().addHandler(logging.StreamHandler())
        logging.getLogger().setLevel(settings.LOGGING_LEVEL)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = loop.run_until_complete(run_server())
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            logging.info("server is shutting down")
        finally:
            loop.run_until_complete(shutdown_server(app))
            loop.close()
    else:
        GameApp.run(title=f"Noughts & Crosses v{VERSION}")


if __name__ == "__main__":
    main()
