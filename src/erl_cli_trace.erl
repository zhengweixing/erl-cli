-module(erl_cli_trace).
-export([fmt/1]).

fmt({call, {{M, F, A}, Stack}, PI, TS}) ->
    io_lib:format("~n% ~s ~s~n% ~w:~w(~s)~s",
                  [fmt_ts(TS), fmt_pi(PI), M, F, fmt_args(A), fmt_stack(Stack)]);
fmt({retn, {{M, F, Arity}, Val}, PI, TS}) ->
    io_lib:format("~n% ~s ~s~n% ~w:~w/~w -> ~120p",
                  [fmt_ts(TS), fmt_pi(PI), M, F, Arity, Val]);
fmt({send, {Msg, ToPI}, PI, TS}) ->
    io_lib:format("~n% ~s ~s~n% ~s <<< ~120p",
                  [fmt_ts(TS), fmt_pi(PI), fmt_pi(ToPI), Msg]);
fmt({'recv', Msg, PI, TS}) ->
    io_lib:format("~n% ~s ~s~n% <<< ~120p",
                  [fmt_ts(TS), fmt_pi(PI), Msg]);
fmt(Other) ->
    io_lib:format("~n% ~120p", [Other]).

fmt_ts({H, Mi, S, _Us}) ->
    io_lib:format("~2.2.0w:~2.2.0w:~2.2.0w", [H, Mi, S]).

fmt_pi({Pid, Reg}) -> io_lib:format("~w(~p)", [Pid, Reg]);
fmt_pi(Other)      -> io_lib:format("~p", [Other]).

fmt_args(A) when is_integer(A) -> io_lib:format("/~w", [A]);
fmt_args(Args) when is_list(Args) ->
    string:join([io_lib:format("~120p", [Arg]) || Arg <- Args], ", ").

fmt_stack(<<>>) -> "";
fmt_stack(Bin) ->
    Lines    = string:tokens(binary_to_list(Bin), "\n"),
    Filtered = [string:strip(L) || L <- Lines, string:str(L, "cp = ") > 0],
    case Filtered of
        [] -> "";
        _  -> ["\n%   " | string:join(Filtered, "\n%   ")]
    end.
