"""Turn the teacher's topic selections into an explicit exam content boundary."""


def build_exam_scope(requested_topics, custom_topic="", custom_subtopic="", has_upload=False):
    lines = [
        "REQUESTED CONTENT SCOPE (STRICT, FOR EVERY QUESTION AND ITS FULL SOLUTION):",
        "Test ONLY the topics and skills explicitly requested below. A topic being in the same year, course, textbook, sample, or syllabus does not make it requested.",
        "Do not introduce an unselected chapter or subtopic as the main task, a crossover, an application, or a hidden prerequisite in the working or answer. Use routine previously taught arithmetic and algebra only as supporting skills, never as a separate tested topic.",
        "Before drafting each question, trace the complete expected solution and remove or replace it if any step tests unselected content. This rule applies at every difficulty, including Extremely Hard.",
    ]
    for topic in requested_topics:
        name = topic["name"]
        course = " ".join(part for part in (topic.get("year"), topic.get("level")) if part)
        selected = topic.get("selected_subtopics") or []
        all_subtopics = topic.get("all_subtopics") or []
        if selected:
            detail = "ONLY these subtopics: " + "; ".join(selected)
        elif all_subtopics:
            detail = "all subtopics in this chapter: " + "; ".join(all_subtopics)
        else:
            detail = "this named topic only"
        lines.append(f"- {course}: {name} — {detail}.")

    if custom_topic.strip():
        lines.append(f"- Teacher-requested custom topic: {custom_topic.strip()}.")
    if custom_subtopic.strip():
        lines.append(f"- Teacher-requested custom subtopic: {custom_subtopic.strip()}.")
    if has_upload:
        lines.append("- Uploaded assessment scope may narrow these selections. If no topic was selected or typed, use only the topics explicitly specified in the uploaded document.")

    # Cambridge's Year 12 Advanced text puts these integration techniques in
    # later chapters. Selecting Chapter 4 alone must not silently include them.
    advanced = [t for t in requested_topics if t.get("year") == "Year 12" and t.get("level") == "Advanced"]
    if any(t.get("name") == "Integration" for t in advanced):
        def requested(chapter, section_prefixes):
            return any(
                t.get("name") == chapter and (
                    not t.get("selected_subtopics") or
                    any(s.startswith(section_prefixes) for s in t["selected_subtopics"])
                ) for t in advanced
            )

        exclusions = []
        if not requested("Exponential Functions", ("5D",)):
            exclusions.append("integrals of exponential functions or exponential integration applications")
        if not requested("Logarithmic Functions", ("6D",)):
            exclusions.append("integrals of reciprocal functions, logarithmic antiderivatives, or logarithmic integration applications")
        if not requested("The Trigonometric Functions", ("7D",)):
            exclusions.append("integrals of trigonometric functions or trigonometric integration applications")
        if not requested("Continuous Probability Distributions", ("17B", "17C", "17D", "17E", "17F")):
            exclusions.append("continuous random variables, probability density functions, or distribution/CDF questions")
        if exclusions:
            lines.append("YEAR 12 ADVANCED INTEGRATION CROSSOVER EXCLUSIONS: Do not require " + "; ".join(exclusions) + ".")

    if not requested_topics and not custom_topic.strip() and not custom_subtopic.strip() and not has_upload:
        lines.append("Use only the topic stated in the exam focus.")
    return "\n".join(lines)
