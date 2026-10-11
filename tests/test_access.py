from bot import access as acl


def test_parse_and_format_people():
    people = acl.parse_people("1024185305 @Boss, @kate; bad!token, 4242")
    assert people == [["1024185305", "@boss"], ["@kate"], ["4242"]]
    assert acl.format_people(people) == "1024185305 @boss, @kate, 4242"
    assert acl.parse_people("") == []


def test_roles_and_summary_target():
    a = acl.Access(admins={111}, owner=[["@boss"]], users=[["222"], ["@kate"]])
    assert a.allowed(111, None) and a.is_admin(111)
    assert a.allowed(999, "Boss") and a.is_owner(999, "boss")
    assert a.allowed(222, None) and a.allowed(5, "KATE")
    assert not a.allowed(333, "stranger") and not a.allowed(333, None)
    assert a.summary_chat_ids() == [111]  # owner has no ID yet -> admin
    a.owner = acl.with_id(a.owner, 999, "Boss")
    assert a.owner == [["999", "@boss"]]
    assert a.summary_chat_ids() == [999]


def test_add_remove_whole_person():
    people = acl.with_id(acl.add([], [["@kate"], ["@sam"]]), 500, "kate")
    assert people == [["500", "@kate"], ["@sam"]]
    assert acl.add(people, [["500"]]) == people  # already there
    assert acl.remove(people, ["500"]) == [["@sam"]]  # removing by ID drops her @username too
    assert acl.remove(people, ["@sam"]) == [["500", "@kate"]]
