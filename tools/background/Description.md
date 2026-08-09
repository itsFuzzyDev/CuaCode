---
name: background
output:
  jobs: list
  job: object
active: True
---
Look in on calls that are running without you.

A job gets here one of two ways. You started it yourself, by passing
background: true to a tool that offers it — worth doing when the call is slow
and you have something else to get on with, and not worth doing when the next
thing you do depends on the answer. Or the user pushed a call you were already
waiting on into the background, in which case you got a job id back where you
expected a result and the work is still going.

Either way the call is unchanged: same tool, same arguments, same result when
it lands. The only difference is that you were handed an id instead of made to
wait.

Actions:

  list    every job and its state — running, killing, done, error — with how
          long each has been going. Pass state to filter.
  output  the result of one job. A job still running comes back with its state
          and no result key; that is not an empty result, it is not finished.
          Reading is free and repeatable.
  kill    ask a running job to stop. Cooperative: a tool that watches for it
          stops within a moment, one that does not runs to the end and its
          result is discarded. Reports which of the two happened.
  wait    block until one job finishes, up to timeout seconds (default 30).
          Use it when you have run out of other work; polling list in a loop
          just burns rounds.

You are told once, between rounds, when a job finishes — a line naming the id,
not the result. Fetch what you actually want to read. If nothing you have been
asked to do depends on a finished job, you do not have to read it at all; say
it finished and move on.

Job ids are per run of the app. A job does not survive the app closing, and
nothing here reaches back into an earlier session.
