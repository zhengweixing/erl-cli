-module(erl_cli_app).
-behaviour(application).
-export([start/2, stop/1]).

start(_StartType, _StartArgs) ->
    {ok, Sup} = erl_cli_sup:start_link(),
    case application:get_env(erl_cli, port) of
        {ok, Port} when is_integer(Port) ->
            Path = application:get_env(erl_cli, path, "/debug"),
            erl_cli:start_server(Port, Path);
        _ -> ok
    end,
    {ok, Sup}.

stop(_State) ->
    cowboy:stop_listener(erl_cli_http).
