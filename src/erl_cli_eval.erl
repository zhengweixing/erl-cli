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

dispatch(Id, ConnPid,
         [{call, _, {remote, _, {atom,_,file_download}, {atom,_,read}}, ArgExprs}]) ->
    case eval_args(ArgExprs) of
        {ok, [FilePath]} -> file_download(Id, ConnPid, FilePath);
        {error, R}       -> reply_error(ConnPid, Id, fmt(R))
    end;

dispatch(Id, ConnPid, Exprs) ->
    try
        {value, Result, _} = erl_eval:exprs(Exprs, []),
        reply(ConnPid, Id, #{<<"result">> => term_to_bin(Result)})
    catch
        Class:Reason ->
            reply_error(ConnPid, Id, iolist_to_binary(io_lib:format("~p:~p", [Class, Reason])))
    end.

reply(ConnPid, Id, Map) ->
    Payload = try
        jiffy:encode(Map#{<<"id">> => Id})
    catch
        _:_ ->
            %% Fallback: jiffy rejected something (e.g. a non-UTF-8 binary).
            %% Format binary values as printable Erlang terms and retry.
            SafeMap = maps:map(fun(_K, V) when is_binary(V) ->
                                       iolist_to_binary(io_lib:format("~p", [V]));
                                  (_K, V) -> V
                               end, Map#{<<"id">> => Id}),
            jiffy:encode(SafeMap)
    end,
    ConnPid ! {debug_reply, Payload},
    ok.

reply_error(ConnPid, Id, Msg) ->
    reply(ConnPid, Id, #{<<"error">> => Msg}).

%% Read a file on the node and return it base64-encoded to the client.
file_download(Id, ConnPid, FilePath) ->
    try
        PathStr = case FilePath of
            B when is_binary(B) -> binary_to_list(B);
            L when is_list(L)   -> L
        end,
        case filelib:is_regular(PathStr) of
            true ->
                case file:read_file(PathStr) of
                    {ok, Content} ->
                        reply(ConnPid, Id, #{
                            <<"type">> => <<"file_download">>,
                            <<"path">> => iolist_to_binary(PathStr),
                            <<"size">> => byte_size(Content),
                            <<"data">> => base64:encode(Content)
                        });
                    {error, Reason} ->
                        reply_error(ConnPid, Id, iolist_to_binary(
                            io_lib:format("read file failed: ~p", [Reason])))
                end;
            false ->
                reply_error(ConnPid, Id, iolist_to_binary(
                    io_lib:format("file not found or not a regular file: ~s", [PathStr])))
        end
    catch
        Class:Err:Stack ->
            reply_error(ConnPid, Id, iolist_to_binary(
                io_lib:format("~p:~p~n~p", [Class, Err, Stack])))
    end.

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
