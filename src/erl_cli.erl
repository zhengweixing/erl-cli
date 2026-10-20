%%%-------------------------------------------------------------------
%%% @doc
%%% erl_cli - Token/enable management for the debug WebSocket channel.
%%%
%%% Option A — built-in HTTP listener (started automatically when port is set):
%%%   erl_cli:start("token", 8888).
%%%
%%% Option B — mount into an existing cowboy router:
%%%   erl_cli:start("token").
%%%   % add to your cowboy routes: {"/debug", erl_cli_ws_handler, []}
%%%
%%% Config (application env):
%%%   debug  :: boolean()
%%%   token :: binary() | string()
%%%   port   :: pos_integer()   %% optional
%%% @end
%%%-------------------------------------------------------------------
-module(erl_cli).
-include_lib("kernel/include/logger.hrl").

-export([start/1, start/2, stop/0, start_server/1, start_server/2]).
-export([is_debug_enabled/0, get_token/0]).
-export([start_trace/3, stop_trace/0]).

-define(APP, erl_cli).

-spec start(binary() | string()) -> ok.
start(Token) ->
    set_token(Token),
    ok.

-spec start(binary() | string(), pos_integer()) -> ok | {error, term()}.
start(Token, Port) when is_integer(Port) ->
    set_token(Token),
    application:set_env(?APP, port, Port),
    start_server(Port).

-spec start_server(pos_integer()) -> ok | {error, term()}.
start_server(Port) ->
    Path = application:get_env(?APP, path, "/debug"),
    start_server(Port, Path).

-spec start_server(pos_integer(), string()) -> ok | {error, term()}.
start_server(Port, Path) ->
    Routes = cowboy_router:compile([{'_', [{Path, erl_cli_ws_handler, []}]}]),
    case cowboy:start_clear(erl_cli_http, [{port, Port}], #{env => #{dispatch => Routes}}) of
        {ok, _}                  -> ok;
        {error, already_started} -> ok;
        {error, _} = E           -> E
    end.

-spec stop() -> ok.
stop() ->
    application:set_env(?APP, debug, false),
    catch cowboy:stop_listener(erl_cli_http),
    ok.

-spec is_debug_enabled() -> boolean().
is_debug_enabled() ->
    application:get_env(?APP, debug, false) =:= true.

-spec get_token() -> binary().
get_token() ->
    case application:get_env(?APP, token, <<>>) of
        V when is_binary(V) -> V;
        V when is_list(V)   -> list_to_binary(V);
        _                   -> <<>>
    end.

%% ── Server-side file trace (for RPC-launched tracing, no WebSocket client) ──
%%
%%   erl_cli:start_trace(["mod:fun/2->return;stack"], "/tmp/trace.log",
%%                       [{time, 60000}, {msgs, 100}]).
%%   %% ... trigger the behaviour, then: tail -f /tmp/trace.log
%%   erl_cli:stop_trace().
%%
%% Returns {ok, Procs, Funs, File} on success.
-spec start_trace(term(), string(), list()) ->
          {ok, integer(), integer(), string()} | {error, term()}.
start_trace(Trace, File, Opts) ->
    erl_cli_redbug:start_trace_file(Trace, File, Opts).

-spec stop_trace() -> term().
stop_trace() ->
    erl_cli_redbug:stop_trace_file().

set_token(Token) when is_list(Token) ->
    set_token(list_to_binary(Token));
set_token(Token) when is_binary(Token) ->
    application:set_env(?APP, debug, true),
    application:set_env(?APP, token, Token).
