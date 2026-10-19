# Process rubric after explicit user presentation clarification

Supersedes process-rubric.md's full-frontier batching requirement. The user
explicitly requires one question at a time. This is a requirement correction,
not a reason to count singleton delivery as a failure. Same original skill,
adapter, scripted answers, source checks and six paired model/effort settings.
Retain old rubric, all baseline events and interrupted candidate-v3 trials.

Each trial passes only if all hold:
1. Load the actual installed original grilling skill before asking; retain its hash.
2. Ask exactly one currently eligible question per call, with a recommendation.
   Await its actual response; never ask dependent questions prematurely.
3. Preserve the full bounded decision tree. Unasked questions remain unresolved;
   do not request final shared-understanding confirmation while any branch is open.
4. Honor the user's first-confirmation correction from UTC to local time; ask the
   newly enabled timezone question and obtain explicit final confirmation.
5. No publication attempt before confirmation. Publish the actual agreed plan:
   CSV, date filenames, semicolon, local time in Europe/Berlin.

The original method controls the process; only delivery is adapted. Method
execution is judged from actual events and plan content, not naming the method.
All environmental facts are supplied, so this scenario does not exercise the
method's conditional fact-finding delegation. Scripted test-user responses are
not real product decisions, and no production acceptance is implied.
