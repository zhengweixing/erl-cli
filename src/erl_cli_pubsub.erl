-module(erl_cli_pubsub).
-behaviour(gen_server).

%% All three public functions run in the caller's process:
%%   - subscribe/unsubscribe write directly to the public ETS table
%%   - publish reads ETS and sends messages without any gen_server hop
%%
%% The gen_server's only job is to own and initialise the ETS table,
%% and clean up dead pids via process monitors set by subscribe/2.

-export([start_link/0, subscribe/2, unsubscribe/2, publish/2]).
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2]).

-define(SERVER, ?MODULE).

%% ETS table: {Topic :: binary(), ConnPid :: pid()}  (bag, public)
-define(TAB, erl_cli_pubsub_tab).

start_link() ->
    gen_server:start_link({local, ?SERVER}, ?MODULE, [], []).

-spec subscribe(binary(), pid()) -> ok.
subscribe(Topic, ConnPid) ->
    case application:get_env(erl_cli, pubsub_mod) of
        {ok, Mod} -> catch Mod:subscribe(Topic, ConnPid);
        _ ->
            gen_server:call(?SERVER, {monitor, ConnPid}),
            ets:insert(?TAB, {Topic, ConnPid})
    end,
    ok.

-spec unsubscribe(binary(), pid()) -> ok.
unsubscribe(Topic, ConnPid) ->
    case application:get_env(erl_cli, pubsub_mod) of
        {ok, Mod} -> catch Mod:unsubscribe(Topic, ConnPid);
        _ -> ets:delete_object(?TAB, {Topic, ConnPid})
    end,
    ok.

-spec publish(binary(), term()) -> ok.
publish(Topic, Data) ->
    case application:get_env(erl_cli, pubsub_mod) of
        {ok, Mod} ->
            catch Mod:publish(Topic, Data);
        _ ->
            Json = jiffy:encode(#{<<"topic">> => Topic, <<"data">> => Data}),
            [ConnPid ! {pubsub_push, Json} || {_T, ConnPid} <- ets:lookup(?TAB, Topic)]
    end,
    ok.

%% ── gen_server ────────────────────────────────────────────────────────────

init([]) ->
    ets:new(?TAB, [named_table, bag, public]),
    {ok, #{}}.

%% Only task left for the gen_server: set monitors on behalf of subscribers
%% so DOWN messages are delivered here for cleanup.
handle_call({monitor, Pid}, _From, State) ->
    monitor(process, Pid),
    {reply, ok, State};

handle_call(_Req, _From, State) -> {reply, ok, State}.
handle_cast(_Msg, State)        -> {noreply, State}.

handle_info({'DOWN', _Ref, process, Pid, _Reason}, State) ->
    ets:match_delete(?TAB, {'_', Pid}),
    {noreply, State};

handle_info(_Info, State) -> {noreply, State}.

terminate(_Reason, _State) -> ok.
