
import os
from crewai import Agent, Task, Crew, LLM
from langchain.tools import tool
import ollama
from langchain.llms.ollama import Ollama
import json
from typing import List, Dict, Any, Optional
from langchain_community.graphs import Neo4jGraph
from langchain.prompts.prompt import PromptTemplate
from langchain.chains import GraphCypherQAChain
import numpy as np
from neo4j import GraphDatabase
import faiss
from sentence_transformers import SentenceTransformer
from dataclasses import dataclass
from collections import defaultdict
import textwrap
import openai
from crewai import Crew, Task, Agent, Process
from langchain.chat_models import ChatOpenAI
import sys

NEO4J_URI = os.getenv('NEO4J_URI')
NEO4J_USERNAME = os.getenv('NEO4J_USERNAME')
NEO4J_PASSWORD = os.getenv('NEO4J_PASSWORD')
NEO4J_DATABASE = os.getenv('NEO4J_DATABASE')
client = LLM(model=os.getenv('LLM_MODEL'), base_url=os.getenv('LLM_BASE_URL'))

def analyzer_agent(problem_description):
    analyzer = Agent(
        role='Problem Analysis Agent',
        goal="""To provide comprehensive problem analysis for competitive programming questions by breaking down complex problems into clear, actionable insights.""",
        backstory="""You are an elite competitive programming analyst with expertise in algorithmic problem-solving and pattern recognition.
        Your analytical skills have been honed through years of experience in prestigious programming competitions and teaching advanced algorithms.
        You specialize in identifying the core essence of problems and breaking them down into their fundamental components.""",
        verbose=True,
        allow_delegation=False,
        llm=client
    )

    analysis_task = Task(
        description=f"""
        Analyze the following competitive programming problem with extreme attention to detail: {problem_description}

        Your analysis must include:
        1. Problem Understanding:
           - Core objective in simple terms
           - Key requirements and constraints
           - Hidden implications and assumptions

        2. Key Observations:
           - Mathematical patterns or properties
           - Edge cases and corner scenarios
           - Optimization opportunities
           - Time and space complexity requirements

        3. Input/Output Analysis:
           - Detailed input format and constraints
           - Output requirements and formatting
           - Size limitations and performance bounds
           - Data range considerations

        4. Underlying Concepts:
           - Primary algorithmic paradigms
           - Related problem categories
           - Potential solution approaches (without implementation)
           - Mathematical foundations
        """,
        expected_output="""A structured, comprehensive analysis document covering all required sections with clear, actionable insights.
        Each section should provide specific, concrete details rather than general observations.
        Include examples where relevant to illustrate key points.""",
        agent=analyzer
    )

    crew = Crew(
        agents=[analyzer],
        tasks=[analysis_task]
    )

    return crew.kickoff().raw

def designer_agent(problem_description, analysis_result):
    designer = Agent(
        role='Test Case Generation Agent',
        goal="""To create comprehensive, high-quality test cases that thoroughly validate competitive programming solutions across all possible scenarios and edge cases.""",
        backstory="""You are an elite competitive programmer with a perfect track record in test case generation.
        Your test cases are known for their completeness, covering edge cases that others miss, and your ability to identify corner cases that break naive solutions.
        You have extensive experience in creating test data that validates both correctness and performance requirements.""",
        verbose=True,
        allow_delegation=False,
        llm=client
    )

    test_case_task = Task(
        description=f"""

        Based on this question: {problem_description}
        and problem analysis: {analysis_result}

        Generate a comprehensive set of test cases including:
        1. Basic Test Cases:
           - Minimal valid inputs
           - Simple cases testing core functionality
           - Standard cases covering common scenarios

        2. Edge Cases:
           - Boundary conditions
           - Minimal and maximal input sizes
           - Special number cases (0, 1, -1, etc.)

        3. Corner Cases:
           - Unusual input combinations
           - Cases that might break naive solutions
           - Performance stress tests

        4. Random Test Cases:
           - Large random inputs within constraints
           - Probabilistic edge cases
           - Performance validation cases
        """,
        expected_output="""A JSON object containing diverse test cases, each with clear inputs, expected outputs, and explanations.
        Test cases should be organized by category and complexity.
        Each test case should include a description of what it's testing and why it's important.""",
        agent=designer
    )

    crew = Crew(
        agents=[designer],
        tasks=[test_case_task]
    )

    return crew.kickoff().raw

class CompetitiveProgrammingSearch:
    def __init__(self, neo4j_uri: str, neo4j_username: str, neo4j_password: str, config={"driver_fetch_size": 1024}):
        self.driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_username, neo4j_password))
        self.model = SentenceTransformer("all-MiniLM-L6-v2")
        self.index = None
        self.questions_data = None
        self.approach_embeddings = []

    def _fetch_questions_by_subtopics(self, subtopics: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch questions and related nodes from Neo4j based on FileNode subtopics.
        """
        cypher_query = """
        MATCH (f:FileNode)
        WHERE f.name IN $subtopics
        MATCH (f)-[:CONTAINS]->(q:Question)
        OPTIONAL MATCH (q)-[:HAS_TESTCASE]->(testCase:TestCase)
        OPTIONAL MATCH (q)-[:OPTIMIZED_BY]->(optimizedApproach:Approach)
        OPTIONAL MATCH (q)-[:APPROACHED_BY]->(approachedApproach:Approach)
        OPTIONAL MATCH (q)-[:HAS_CONSTRAINT]->(constraint:Constraint)
        OPTIONAL MATCH (q)-[:IMPROVED_BY]->(improvedApproach:Approach)
        OPTIONAL MATCH (optimizedApproach)-[:HAS_CODE]->(optimizedCode:Code)
        OPTIONAL MATCH (approachedApproach)-[:HAS_CODE]->(approachedCode:Code)
        OPTIONAL MATCH (improvedApproach)-[:HAS_CODE]->(improvedCode:Code)
        RETURN q,
               collect(testCase) AS testCases,
               collect(optimizedApproach) AS optimizedApproaches,
               collect(approachedApproach) AS approachedApproaches,
               collect(constraint) AS constraints,
               collect(improvedApproach) AS improvedApproaches,
               collect(optimizedCode) AS optimizedCodes,
               collect(approachedCode) AS approachedCodes,
               collect(improvedCode) AS improvedCodes
        """
        with self.driver.session() as session:
            results = session.run(cypher_query, subtopics=subtopics, **{"fetch_size": 1024}).data()
            return results

    def _create_weighted_embeddings(self, questions_data: List[Dict[str, Any]], code_analysis: str):
        """Create separate embeddings for each approach and store them in self.approach_embeddings"""
        self.approach_embeddings = []

        for q_data in questions_data:
            # Approached Approaches
            for approach, code in zip(q_data['approachedApproaches'], q_data['approachedCodes']):
                if approach:
                    approach_components = [approach['description']]
                    if code:
                        approach_components.append(code['content'])
                    approach_text = " ".join(filter(None, approach_components))
                    approach_embedding = self.model.encode([approach_text])[0]
                    self.approach_embeddings.append({
                        'question_id': q_data['q']['text'],
                        'approach_type': 'approached',
                        'embedding': approach_embedding,
                        'approach_node': approach,
                        'code_node': code
                    })

            # Optimized Approaches
            for approach, code in zip(q_data['optimizedApproaches'], q_data['optimizedCodes']):
                if approach:
                    approach_components = [approach['description']]
                    if code:
                        approach_components.append(code['content'])
                    approach_text = " ".join(filter(None, approach_components))
                    approach_embedding = self.model.encode([approach_text])[0]
                    self.approach_embeddings.append({
                        'question_id': q_data['q']['text'],
                        'approach_type': 'optimized',
                        'embedding': approach_embedding,
                        'approach_node': approach,
                        'code_node': code
                    })

            # Improved Approaches
            for approach, code in zip(q_data['improvedApproaches'], q_data['improvedCodes']):
                if approach:
                    approach_components = [approach['description']]
                    if code:
                        approach_components.append(code['content'])
                    approach_text = " ".join(filter(None, approach_components))
                    approach_embedding = self.model.encode([approach_text])[0]
                    self.approach_embeddings.append({
                        'question_id': q_data['q']['text'],
                        'approach_type': 'improved',
                        'embedding': approach_embedding,
                        'approach_node': approach,
                        'code_node': code
                    })
        return None

    def fetch_related_approaches(self, query_embedding, top_k=3, approach_types=None):
        """Fetch top_k related approaches based on embedding similarity to query_embedding."""
        if not self.approach_embeddings:
            return []

        relevant_approaches = []
        for approach_data in self.approach_embeddings:
            if approach_types is None or approach_data['approach_type'] in approach_types:
                embedding_vector = approach_data['embedding']
                similarity_score = np.dot(query_embedding, embedding_vector)
                relevant_approaches.append({'approach_data': approach_data, 'similarity': similarity_score})

        relevant_approaches.sort(key=lambda item: item['similarity'], reverse=True)
        return relevant_approaches[:top_k]

    def find_similar_approaches(self,
                              query_text: str,
                              code_analysis: str,
                              subtopics: List[str],
                              k: int = 5) -> List[Dict[str, Any]]:
        """
        Finds the top k most similar approaches to the given query text.
        """
        # Fetch questions data for the specified subtopics
        questions_data = self._fetch_questions_by_subtopics(subtopics)

        if questions_data:
            # Create weighted embeddings for approaches
            self._create_weighted_embeddings(questions_data, code_analysis)

            # Embed the query text
            query_embedding = self.model.encode([query_text])[0]

            # Fetch top k similar approaches
            similar_approaches_data = self.fetch_related_approaches(query_embedding, top_k=k)

            results = []
            for item in similar_approaches_data:
                approach_data = item['approach_data']
                similarity_score = item['similarity']
                results.append({
                    'question_id': approach_data['question_id'],
                    'approach_node': approach_data['approach_node'],
                    'code_node': approach_data['code_node'],
                    'similarity_score': similarity_score
                })
            return results
        else:
            print(f"No questions found for subtopics: {subtopics}")
            return []

def create_planner(problem_description, problem_analysis, similar_approach):
    planner = Agent(
        role='Planning Agent',
        goal="""To create detailed, step-by-step solution plans for competitive programming problems.""",
        backstory="""You are a master algorithmic strategist with deep expertise in competitive programming.
        Your expertise lies in recognizing patterns, optimizing logic, and ensuring clarity in implementation plans for competitve coding questions""",
        verbose=True,
        allow_delegation=False,
        llm=client
    )

    planning_task = Task(
        description=f"""
        Given a current problem statement and a reference approach for a related problem, you must:
        Understand the problem constraints, input-output structure, and edge cases.
        Analyze the provided related approach, identifying similarities and necessary modifications.
        Create a solution plan to help here is a potetnially useful approach : {similar_approach}
        Given this current problem statement: {problem_description}
        and Given this problem analysis: {problem_analysis}
        feel free to use them if needed. Please reason deeply how to solve the problem and write concise and clear verbal solution for it.
        Don't write the code, just verbal explanation in a couple of paragraphs. Only professional competitive programmers will read your solution, so feel free to refer to advanced algorithms
        """,
        expected_output="""A structured solution plan document containing the  algor.""",
        agent=planner
    )

    crew = Crew(
        agents=[planner],
        tasks=[planning_task]
    )

    return crew.kickoff().raw

def coder_agent(problem_description, solution_plan, similar_code):
    coder = Agent(
        role='Coding Agent',
        goal="""To implement optimal, efficient, and correct solutions for competitive programming problems.""",
        backstory="""You are an elite competitive programmer with a perfect track record in implementing complex algorithms.""",
        verbose=True,
        allow_delegation=False,
        llm=client
    )

    coding_task = Task(
        description=f"""
        Given the problem statement : {problem_description} and potential plan : {solution_plan}
        Translate the plan into an optimized implementation in Python.
        Use it if needed.
        Implement a complete solution.
        """,
        expected_output="""Complete code implementation that follows the solution plan and meets all requirements passing all test cases.""",
        agent=coder
    )

    crew = Crew(
        agents=[coder],
        tasks=[coding_task]
    )

    return crew.kickoff().raw

def solve_competitive_programming_problem(problem_description: str):

    analysis_result = analyzer_agent(problem_description)
    print("Analysis completed.")

    designer_output = designer_agent(problem_description, analysis_result)
    tags = [
    "Array",
    "Binary Search",
    "Binary Tree and Binary Search Tree",
    "Bit Manipulation",
    "Dynamic Programming",
    "Graphs",
    "Greedy",
    "Heaps",
    "Linked List",
    "Recursion",
    "Strings",
    "Tries",
    "Two Pointer"]
    # tags = create_tagger(topics, analysis_result)
    # tags = tags.split('$')

    print("Test cases and tags generated.")

    search = CompetitiveProgrammingSearch(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD)

    # Retrieve top 5 similar approaches
    similar_approaches = search.find_similar_approaches(
        query_text=problem_description,
        code_analysis=analysis_result,
        subtopics=tags,
        k=5
    )
    print("Similar approaches retrieved.")

    # Initialize results list to store outputs
    results = []

    # Iterate through each approach
    for i, approach in enumerate(similar_approaches[:5]):
        print(f"Processing approach {i + 1}...")

        # Extract approach and corresponding code
        approach_data = approach['approach_node']
        code_data = approach['code_node']

        # Create a solution plan using the Planner Agent
        solution_plan = create_planner(problem_description, analysis_result, approach_data)
        print(f"Plan {i + 1} created.")

        # Generate code using the Coder Agent with the corresponding code node
        final_solution = coder_agent(problem_description, solution_plan, code_data)
        print(f"Solution {i + 1} coded.")

        # Store results
        results.append({
            'approach_index': i + 1,
            'analysis': analysis_result,
            # 'test_cases': designer_output,
            #'tags': tags,
            #'similar_approach': approach,
            'solution_plan': solution_plan,
            'final_solution': final_solution
        })

    # Return final results containing all 5 processed approaches
    return results


if __name__ == "__main__":

    problem = " ".join(sys.argv[1:]) # Capture the problem from cmd

    result = solve_competitive_programming_problem(problem)
    