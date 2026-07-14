from src.narrator.narrator import final_line, goal_line


def test_goal_line_has_score_and_teams():
    line = goal_line("França x Espanha", 1, 0)
    assert "França" in line and "Espanha" in line
    assert "1" in line and "0" in line


def test_goal_line_credits_scoring_side():
    assert "Espanha" in goal_line("França x Espanha", 0, 1)


def test_final_line_names_leader():
    line = final_line("França x Espanha", 2, 1, "Crash")
    assert "Crash" in line and "2" in line and "1" in line


def test_lines_survive_label_without_x():
    assert goal_line("jogo 18237038", 1, 0)
    assert final_line("jogo 18237038", 1, 1, "Ana")
