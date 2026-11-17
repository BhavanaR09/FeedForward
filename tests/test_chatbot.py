from app import app


def test_chatbot_service_has_inspection_guidance():
    from chatbot_service import generate_chatbot_reply

    response = generate_chatbot_reply('food inspection approval rejected delivery')
    assert 'Inspection Completed – Approved' in response or 'Inspection Completed - Approved' in response
    assert 'rejected' in response.lower()


def test_chatbot_route_returns_json_message():
    client = app.test_client()
    response = client.post('/chatbot', json={'message': 'How does donation work?'})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert 'message' in payload
    assert isinstance(payload['message'], str)
    assert len(payload['message']) > 0


def test_chatbot_distinguishes_typed_intents():
    from chatbot_service import generate_chatbot_reply

    messages = [
        'Can I get a certification for my donations?',
        'How can I donate food?',
        'How can I donate clothes?',
        'How can I donate books?',
        'How can I donate money?',
        'Thank you',
    ]
    replies = [generate_chatbot_reply(message) for message in messages]

    assert len(set(replies)) == len(messages)
    assert 'certificate' in replies[0].lower()
    assert 'food' in replies[1].lower()
    assert 'clothes' in replies[2].lower()
    assert 'books' in replies[3].lower()
    assert 'money' in replies[4].lower()
    assert 'welcome' in replies[5].lower()


def test_chatbot_requested_intent_examples():
    from chatbot_service import generate_chatbot_reply

    cases = {
        'can i get the certificaions': 'certificate',
        'can i get a certificate': 'certificate',
        'how money donation will work': 'money',
        'how donation types are there': 'four donation types',
        'how can I donate food': 'food',
        'how does food inspection work': 'inspection',
        'how can I become a volunteer': 'volunteer',
        'hello': 'hi!',
    }

    replies = [generate_chatbot_reply(message) for message in cases]
    for reply, expected in zip(replies, cases.values()):
        assert expected in reply.lower()

    assert 'generic fallback' not in replies[0].lower()
    assert len(set(replies)) == len(cases)


def test_chatbot_handles_volunteer_certificates():
    from chatbot_service import generate_chatbot_reply

    volunteer_questions = [
        'Can volunteers get certificates?',
        'Do volunteers get certificates?',
        'How can I get a volunteer certificate?',
        'Can I get a certificate for volunteering?',
        'volunteer certification',
        'volunteer certificate',
    ]
    replies = [generate_chatbot_reply(question) for question in volunteer_questions]

    assert all('volunteer' in reply.lower() and 'certificate' in reply.lower() for reply in replies)
    assert all('activity' in reply.lower() or 'deliver' in reply.lower() for reply in replies)


def test_chatbot_certificate_response_covers_donors_and_volunteers():
    from chatbot_service import generate_chatbot_reply

    response = generate_chatbot_reply('Can I get a certificate for my donation?')

    assert 'donor' in response.lower()
    assert 'volunteer' in response.lower()
    assert 'certificate' in response.lower()


def test_chatbot_professional_knowledge_intents():
    from chatbot_service import generate_chatbot_reply

    cases = {
        'How does FeedForward work?': 'register',
        'What donation types are available?': 'four donation types',
        'How does the leaderboard work?': 'leaderboard',
        'Where will I receive my OTP?': 'verification otp',
        'Can money donations be inspected?': 'food-safety inspection applies only to food',
        'How can an NGO participate?': 'ngo',
    }

    for question, expected in cases.items():
        assert expected in generate_chatbot_reply(question).lower()


def test_chatbot_follow_up_context():
    from chatbot_service import generate_chatbot_reply

    certificate_follow_up = generate_chatbot_reply(
        'Where can I find it?',
        context='Can volunteers get certificates?'
    )
    money_follow_up = generate_chatbot_reply(
        'Does it require inspection?',
        context='How does money donation work?'
    )

    assert 'volunteer dashboard' in certificate_follow_up.lower()
    assert 'money donations' in money_follow_up.lower()
    assert 'food inspection' in money_follow_up.lower()
