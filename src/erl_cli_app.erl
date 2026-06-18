%%%-------------------------------------------------------------------
%% @doc erl_cli public API
%% @end
%%%-------------------------------------------------------------------

-module(erl_cli_app).

-behaviour(application).

-export([start/2, stop/1]).

start(_StartType, _StartArgs) ->
    erl_cli_sup:start_link().

stop(_State) ->
    ok.

%% internal functions
