#
# Copyright 2026 Brett Adams
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""choose: pick one of several options for each event with the OpenAI Decisions API.

    ... | choose "<question>" <option> <description> <option> <description> [...]
                 [field=<field>] [output=<field>] [error_field=<field>]
                 [model=<model>] [cache=<bool>] [limit=<int>] [distributed=<bool>]

Asks a choice question and adds the chosen option, its probability, the
answer's confidence and every option's probability to each event. Events are
never dropped. The shared settings, cache and streaming code is in
decisions_command.
"""

import sys

import import_declare_test  # noqa: F401  (puts the add-on's lib/ on sys.path)
from splunklib.searchcommands import Configuration, Option, dispatch, validators

from decisions_command import QuestionCommand, probability_fields
from openai_decisions import Choice

EXAMPLE = '| choose "Which team owns this?" network "Routers and DNS" storage "Disks and backups"'


@Configuration(distributed=False)
class ChooseCommand(QuestionCommand):
    """
    ##Syntax
    choose <string> <string> <string> <string> <string>... [field=<field>]
    [output=<field>] [error_field=<field>] [model=<string>] [cache=<bool>]
    [limit=<int>] [distributed=<bool>]

    ##Description
    Asks the OpenAI Decisions API which of the options fits each event and adds
    the chosen option, its probability, the confidence, and the probability of
    each option. Events are never dropped. The question is the first argument,
    in double quotes, followed by two or more options, each a value and then
    its description. At most limit API calls (default 1000, 0 for no limit)
    are made per search; cache hits do not count. With distributed=true, the
    indexers also run the command and call the API themselves, without the
    cache or the limit.
    """

    FROM_CACHE_FIELD = "choice_from_cache"

    output = Option(
        doc="Field that receives the chosen option, and the prefix of the probability "
        "and confidence fields. Default: choice.",
        default="choice",
        validate=validators.Fieldname(),
    )
    error_field = Option(
        doc="Field that receives a short error when an event could not be decided. "
        "Default: choice_error.",
        default="choice_error",
        validate=validators.Fieldname(),
    )

    def _resolve_question(self):
        question, choices, hint = self._question_and_pairs("option", EXAMPLE)
        try:
            choice = Choice(question, choices)
        except ValueError as e:
            self._fail(f"choose: {e}. {hint}")
        self._fields = [
            self.output,
            self.output + "_probability",
            self.output + "_confidence",
            *probability_fields(self.output, choice.options),
        ]
        return choice

    def _write_answer(self, record, answer):
        if answer is None:
            values = [None] * len(self._fields)
        else:
            values = [answer.choice, answer.probability, answer.confidence, *answer.probabilities]
        record.update(zip(self._fields, values))


if __name__ == "__main__":
    dispatch(ChooseCommand, sys.argv, sys.stdin, sys.stdout, __name__)
