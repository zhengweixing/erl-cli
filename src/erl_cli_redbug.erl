-module(erl_cli_redbug).
-behaviour(gen_server).
-include_lib("kernel/include/logger.hrl").

-export([start_link/0, start_trace/4, stop_trace/2]).
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2]).

-record(state, {
    status   = idle :: idle | running,
    conn_pid :: pid() | undefined
}).

start_link() ->
    gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

-spec start_trace(binary(), pid(), term(), list()) -> ok.
start_trace(Id, ConnPid, Trc, UserOpts) ->
    gen_server:call(?MODULE, {start_trace, Id, ConnPid, Trc, UserOpts}).

-spec stop_trace(binary(), pid()) -> ok.
stop_trace(Id, ConnPid) ->
    gen_server:call(?MODULE, {stop_trace, Id, ConnPid}).

init([]) -> {ok, #state{}}.

handle_call({start_trace, Id, ConnPid, Trc, UserOpts}, _From, #state{status = idle} = State) ->
    Self = self(),
    PrintFun = make_print_fun(Id, Self),
    Opts = [{print_fun, PrintFun} | proplists:delete(blocking, proplists:delete(print_fun, UserOpts))],
    case redbug:start(Trc, Opts) of
        {Procs, Funs} ->
            reply(ConnPid, Id, #{<<"status">> => <<"started">>, <<"procs">> => Procs, <<"funs">> => Funs}),
            {reply, ok, State#state{status = running, conn_pid = ConnPid}};
        Error ->
            reply(ConnPid, Id, #{<<"error">> => fmt(Error)}),
            {reply, {error, Error}, State}
    end;

handle_call({start_trace, Id, ConnPid, _Trc, _Opts}, _From, #state{status = running} = State) ->
    reply(ConnPid, Id, #{<<"error">> => <<"redbug already running">>}),
    {reply, {error, already_running}, State};

handle_call({stop_trace, Id, ConnPid}, _From, State) ->
    reply(ConnPid, Id, #{<<"result">> => fmt(redbug:stop())}),
    {reply, ok, State#state{status = idle, conn_pid = undefined}};

handle_call(_Req, _From, State) -> {reply, ok, State}.
handle_cast(_Msg, State)        -> {noreply, State}.

handle_info({trace_event, Json}, #state{conn_pid = ConnPid} = State) when is_pid(ConnPid) ->
    ConnPid ! {debug_reply, Json},
    {noreply, State};

handle_info({trace_done, Json}, #state{conn_pid = ConnPid} = State) when is_pid(ConnPid) ->
    ConnPid ! {debug_reply, Json},
    {noreply, State#state{status = idle, conn_pid = undefined}};

handle_info(_Info, State) -> {noreply, State}.

terminate(_Reason, _State) ->
    catch redbug:stop(),
    ok.

make_print_fun(Id, Owner) ->
    fun({meta, stop, _, _}) ->
            Owner ! {trace_done, jiffy:encode(#{<<"id">> => Id, <<"done">> => true})};
       ({meta, _, _, _}) -> ok;
       (TraceMsg) ->
            try
                Text = iolist_to_binary(erl_cli_trace:fmt(TraceMsg)),
                Owner ! {trace_event, jiffy:encode(#{<<"id">> => Id, <<"trace">> => Text})}
            catch C:R ->
                ?LOG_ERROR("[erl_cli_redbug] fmt error ~p:~p", [C, R])
            end
    end.

reply(ConnPid, Id, Map) ->
    ConnPid ! {debug_reply, jiffy:encode(Map#{<<"id">> => Id})},
    ok.

fmt(V) when is_binary(V) -> V;
fmt(V) when is_atom(V)   -> atom_to_binary(V, utf8);
fmt(V) -> iolist_to_binary(io_lib:format("~p", [V])).
