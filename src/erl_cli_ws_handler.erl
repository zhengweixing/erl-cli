-module(erl_cli_ws_handler).
-include_lib("kernel/include/logger.hrl").
-export([init/2, websocket_init/1, websocket_handle/2, websocket_info/2, terminate/3]).

init(Req, Opts) ->
    case check_auth(Req, Opts) of
        true  -> {cowboy_websocket, Req, [], #{idle_timeout => 300000}};
        false -> {ok, cowboy_req:reply(401, #{}, <<"unauthorized">>, Req), undefined}
    end.

check_auth(Req, Opts) ->
    case application:get_env(erl_cli, auth_mod) of
        {ok, Mod} ->
            Mod:check_auth(Req, Opts);
        _ ->
            erl_cli:is_debug_enabled() andalso
                begin
                    #{token := Token} = cowboy_req:match_qs([{token, [], <<>>}], Req),
                    Token =:= erl_cli:get_token()
                end
    end.

websocket_init(State) -> {ok, State}.

websocket_handle({text, Data}, State) ->
    case jiffy:decode(Data, [return_maps]) of
        #{<<"id">> := Id, <<"beam">> := Mod, <<"data">> := B64} ->
            Self = self(),
            spawn(fun() -> load_beam(Id, Self, Mod, B64) end),
            {ok, State};
        #{<<"id">> := Id, <<"subscribe">> := Topic} ->
            erl_cli_pubsub:subscribe(Topic, self()),
            {reply, {text, jiffy:encode(#{<<"id">> => Id, <<"result">> => <<"subscribed">>})}, State};
        #{<<"id">> := Id, <<"unsubscribe">> := Topic} ->
            erl_cli_pubsub:unsubscribe(Topic, self()),
            {reply, {text, jiffy:encode(#{<<"id">> => Id, <<"result">> => <<"unsubscribed">>})}, State};
        #{<<"id">> := Id, <<"code">> := Code} ->
            Self = self(),
            spawn(fun() -> erl_cli_eval:run(Id, Self, Code) end),
            {ok, State};
        _ ->
            {reply, {text, jiffy:encode(#{<<"error">> => <<"invalid request">>})}, State}
    end;
websocket_handle(_Frame, State) ->
    {ok, State}.

websocket_info({debug_reply, Json}, State)  -> {reply, {text, Json}, State};
%% Forward pubsub push messages to the WebSocket client
websocket_info({pubsub_push, Json}, State)  -> {reply, {text, Json}, State};
websocket_info(_Info, State)                -> {ok, State}.

terminate(_Reason, _Req, _State) -> ok.

%% Load a beam binary (base64-encoded) into the VM via code:load_binary/3.
load_beam(Id, ConnPid, ModBin, B64) ->
    Mod = binary_to_atom(ModBin, utf8),
    Beam = base64:decode(B64),
    Reply = fun(Map) -> ConnPid ! {debug_reply, jiffy:encode(Map#{<<"id">> => Id})} end,
    case code:load_binary(Mod, atom_to_list(Mod) ++ ".beam", Beam) of
        {module, Mod} -> Reply(#{<<"result">> => <<"ok">>});
        {error, What} -> Reply(#{<<"error">> => iolist_to_binary(io_lib:format("~p", [What]))})
    end.
