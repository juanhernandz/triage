from triage.validation_primitives import string_is_tablesafe
from hypothesis import given, example
from hypothesis.strategies import text, characters


# test with a variety of strings based on letters and numbers auto-generated by hypothesis
# and also add a hardcoded example that includes underscores because those are fine
@given(text(alphabet=characters(whitelist_categories=('Lu', 'Ll', 'Nd')), min_size=1))
@example('a_valid_name')
def test_string_is_tablesafe(s):
    assert string_is_tablesafe(s)


# test with a variety of strings based on unsafe characters auto-generated by hypothesis
# and also add a hardcoded example that should be bad because it has spaces
@given(text(alphabet='/ "'))
@example('Spaces are not valid')
def test_string_is_not_tablesafe(s):
    assert not string_is_tablesafe(s)
