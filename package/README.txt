OpenAI Decisions Add-on

Provides three streaming search commands that ask the OpenAI Decisions API a
question about each event:

    decide  a yes/no question; adds the probability of "yes".
    choose  pick one of several options; adds the choice and each option's probability.
    grade   rate against ordered levels; adds the score and each level's probability.

    index=auth | decide "Is this a failed login?" | where decision_probability > 0.8
    sourcetype=tickets | choose "Which team should handle this?" network "Routers and DNS" storage "Disks and backups"
    index=auth | grade "How severe is this?" low "No action needed" high "Investigate now"

Set the OpenAI API key on the add-on's Configuration page. See the project README for
details: https://github.com/Bre77/TA-openai-decisions
