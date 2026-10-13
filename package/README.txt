OpenAI Decisions Add-on

Provides the decide streaming search command, which asks the OpenAI Decisions API a
yes/no question about each event and adds the probability of "yes" as a field:

    index=auth | decide prompt="Is this a failed login?" | where decision_probability > 0.8

Set the OpenAI API key on the add-on's Configuration page. See the project README for
details: https://github.com/Bre77/TA-openai-decisions
