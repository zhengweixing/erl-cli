-module(erl_cli_eval).
-include_lib("kernel/include/logger.hrl").
-export([run/3]).

-spec run(binary(), pid(), binary()) -> ok.
run(Id, ConnPid, CodeBin) ->
    CodeStr = binary_to_list(CodeBin),
    case parse_exprs(CodeStr) of
        {ok, Exprs} -> dispatch(Id, ConnPid, Exprs);
        {error, Reason} ->
            reply_error(ConnPid, Id, iolist_to_binary(io_lib:format("parse error: ~p", [Reason])))
    end.

dispatch(Id, ConnPid,
         [{call, _, {remote, _, {atom,_,redbug}, {atom,_,start}}, ArgExprs}]) ->
    case eval_args(ArgExprs) of
        {ok, [Trc]}       -> erl_cli_redbug:start_trace(Id, ConnPid, Trc, []);
        {ok, [Trc, Opts]} -> erl_cli_redbug:start_trace(Id, ConnPid, Trc, Opts);
        {error, R}        -> reply_error(ConnPid, Id, fmt(R))
    end;

dispatch(Id, ConnPid,
         [{call, _, {remote, _, {atom,_,redbug}, {atom,_,stop}}, []}]) ->
    erl_cli_redbug:stop_trace(Id, ConnPid);

dispatch(Id, ConnPid, Exprs) ->
    try
        {value, Result, _} = erl_eval:exprs(Exprs, []),
        reply(ConnPid, Id, #{<<"result">> => term_to_bin(Result)})
    catch
        Class:Reason ->
            reply_error(ConnPid, Id, iolist_to_binary(io_lib:format("~p:~p", [Class, Reason])))
    end.

reply(ConnPid, Id, Map) ->
    ConnPid ! {debug_reply, jiffy:encode(Map#{<<"id">> => Id})},
    ok.

reply_error(ConnPid, Id, Msg) ->
    reply(ConnPid, Id, #{<<"error">> => Msg}).

parse_exprs(CodeStr) ->
    Dotted = case lists:last(CodeStr) of $. -> CodeStr; _ -> CodeStr ++ "." end,
    case erl_scan:string(Dotted) of
        {ok, Tokens, _} -> erl_parse:parse_exprs(Tokens);
        {error, Info, _} -> {error, Info}
    end.

eval_args(ArgExprs) ->
    try
        Vals = [begin {value, V, _} = erl_eval:expr(A, []), V end || A <- ArgExprs],
        {ok, Vals}
    catch
        Class:Reason -> {error, {Class, Reason}}
    end.

term_to_bin(V) when is_binary(V)  -> V;
term_to_bin(V) when is_atom(V)    -> atom_to_binary(V, utf8);
term_to_bin(V) when is_integer(V) -> integer_to_binary(V);
term_to_bin(V) when is_float(V)   -> float_to_binary(V, [{decimals, 6}, compact]);
term_to_bin(V) -> iolist_to_binary(io_lib:format("~p", [V])).

fmt(V) -> iolist_to_binary(io_lib:format("~p", [V])).
