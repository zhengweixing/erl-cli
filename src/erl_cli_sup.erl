-module(erl_cli_sup).
-behaviour(supervisor).
-export([start_link/0, init/1]).

-define(SERVER, ?MODULE).

start_link() ->
    supervisor:start_link({local, ?SERVER}, ?MODULE, []).

init([]) ->
    Redbug = #{id => erl_cli_redbug,
               start => {erl_cli_redbug, start_link, []},
               restart => permanent,
               shutdown => 5000,
               type => worker},
    Pubsub = #{id => erl_cli_pubsub,
               start => {erl_cli_pubsub, start_link, []},
               restart => permanent,
               shutdown => 5000,
               type => worker},
    {ok, {#{strategy => one_for_one, intensity => 5, period => 10}, [Redbug, Pubsub]}}.
