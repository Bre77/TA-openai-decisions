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
"""grade: grade each event against ordered levels with the OpenAI Decisions API.

    ... | grade "<question>" <label> <description> <label> <description> [...]
                [field=<field>] [output=<field>] [error_field=<field>]
                [model=<model>] [cache=<bool>] [limit=<int>]

Asks the API a score question. The levels go from lowest to highest. Adds the
score (the probability-weighted average of the level indices, from 0 to the
number of levels minus 1), the answer's confidence and every level's
probability to each event. Events are never dropped. The shared settings,
cache and streaming code is in decisions_command.
"""

import sys

import import_declare_test  # noqa: F401  (puts the add-on's lib/ on sys.path)
from splunklib.searchcommands import Configuration, Option, dispatch, validators

from decisions_command import QuestionCommand, probability_fields
from openai_decisions import Score

EXAMPLE = '| grade "How severe is this?" low "No action needed" high "Investigate now"'


@Configuration(distributed=False)
class GradeCommand(QuestionCommand):
    """
    ##Syntax
    grade <string> <string> <string> <string> <string>... [field=<field>]
    [output=<field>] [error_field=<field>] [model=<string>] [cache=<bool>]
    [limit=<int>]

    ##Description
    Grades each event against ordered levels with the OpenAI Decisions API and
    adds the score (the probability-weighted average of the level indices), the
    confidence, and the probability of each level. Events are never dropped.
    The question is the first argument, in double quotes, followed by two or
    more levels from lowest to highest, each a label and then its description.
    At most limit API calls (default 1000, 0 for no limit) are made per search;
    cache hits do not count.
    """

    FROM_CACHE_FIELD = "grade_from_cache"

    output = Option(
        doc="Field that receives the score, and the prefix of the probability and "
        "confidence fields. Default: grade.",
        default="grade",
        validate=validators.Fieldname(),
    )
    error_field = Option(
        doc="Field that receives a short error when an event could not be decided. "
        "Default: grade_error.",
        default="grade_error",
        validate=validators.Fieldname(),
    )

    def _resolve_question(self):
        question, levels, hint = self._question_and_pairs("level", EXAMPLE)
        try:
            score = Score(question, levels)
        except ValueError as e:
            self._fail(f"grade: {e}. {hint}")
        self._fields = [
            self.output,
            self.output + "_confidence",
            *probability_fields(self.output, score.labels),
        ]
        return score

    def _write_answer(self, record, answer):
        if answer is None:
            values = [None] * len(self._fields)
        else:
            values = [answer.score, answer.confidence, *answer.probabilities]
        record.update(zip(self._fields, values))


if __name__ == "__main__":
    dispatch(GradeCommand, sys.argv, sys.stdin, sys.stdout, __name__)
