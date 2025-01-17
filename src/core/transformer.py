import os
import logging
import json
from typing import List, Dict, Optional, Union, Literal

import openai
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from src.utils.text_processor import TextProcessor

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Pre-defined models
LocalModelType = Literal["phi-4", "sky-t1-32b", "deepseek-v3"]
APIProvider = Literal["openai", "gemini"]

# Custom type for model selection
ModelType = Union[LocalModelType, str]  # str for custom API model names

class WordCountError(Exception):
    """Raised when word count requirements are not met"""
    pass

class TranscriptTransformer:
    """Transforms conversational transcripts into teaching material using LLM"""
    
    MAX_RETRIES = 3
    CHUNK_SIZE = 6000
    LARGE_DEVIATION_THRESHOLD = 0.20
    
    # Model IDs for local models
    LOCAL_MODEL_IDS = {
        "phi-4": "microsoft/phi-4",
        "sky-t1-32b": "NovaSky-AI/Sky-T1-32B-Preview",
        "deepseek-v3": "deepseek-ai/DeepSeek-V3"
    }
    
    # Model context limits (in tokens)
    MODEL_LIMITS = {
        # Local models
        "phi-4": 16384,
        "sky-t1-32b": 4096,
        "deepseek-v3": 8192,
        # OpenAI models
        "gpt-3.5-turbo": 4096,
        "gpt-4": 8192,
        "gpt-4-turbo": 128000,
        "gpt-4o-mini": 8192,
        # Gemini models
        "gemini-pro": 32768,
        "gemini-2.0-flash-exp": 128000
    }
    
    def __init__(self, model_type: ModelType = "phi-4", api_provider: Optional[APIProvider] = None, custom_context: Optional[int] = None):
        """
        Initialize the transformer with selected LLM
        
        Args:
            model_type: Model to use. Can be a local model name or custom API model name
            api_provider: If using custom model name, specify the API provider
            custom_context: Custom context limit for models not in predefined list
        """
        self.text_processor = TextProcessor()
        self.model_type = model_type
        self.api_provider = api_provider
        
        # Get model's context limit
        if custom_context is not None:
            self.max_tokens = custom_context
            logger.info(f"Using custom context limit: {self.max_tokens} tokens")
        else:
            self.max_tokens = self.MODEL_LIMITS.get(model_type, 4096)
            logger.info(f"Model {model_type} context limit: {self.max_tokens} tokens")
        
        # Local models
        if model_type in self.LOCAL_MODEL_IDS:
            logger.info(f"Initializing local model: {model_type}")
            self.tokenizer = AutoTokenizer.from_pretrained(self.LOCAL_MODEL_IDS[model_type])
            self.model = AutoModelForCausalLM.from_pretrained(
                self.LOCAL_MODEL_IDS[model_type],
                torch_dtype=torch.float16,
                device_map="auto"
            )
            self.model.eval()
        
        # API models
        elif api_provider:
            if api_provider == "openai":
                if not os.getenv('OPENAI_API_KEY'):
                    raise ValueError("OPENAI_API_KEY not found in environment variables")
                logger.info(f"Initializing OpenAI model: {model_type}")
                self.openai_client = openai.OpenAI(
                    api_key=os.getenv('OPENAI_API_KEY')
                )
                self.model_name = model_type
                
            elif api_provider == "gemini":
                if not os.getenv('GOOGLE_API_KEY'):
                    raise ValueError("GOOGLE_API_KEY not found in environment variables")
                logger.info(f"Initializing Gemini model: {model_type}")
                self.openai_client = openai.OpenAI(
                    api_key=os.getenv('GOOGLE_API_KEY'),
                    base_url="https://generativelanguage.googleapis.com/v1beta"
                )
                self.model_name = model_type
        else:
            raise ValueError("For custom model names, api_provider must be specified")
        
        # Target word counts
        self.words_per_minute = 130

    def _get_safe_max_tokens(self, prompt_tokens: int) -> int:
        """Calculate safe max tokens for generation based on model's context limit"""
        available_tokens = self.max_tokens - prompt_tokens
        # Reserve 10% for safety
        safe_tokens = int(available_tokens * 0.9)
        return max(100, min(safe_tokens, 8000))  # Between 100 and 8000
        
    def _generate_with_local_model(self, prompt: str, max_tokens: Optional[int] = None) -> str:
        """Generate text using local models"""
        inputs = self.tokenizer(prompt, return_tensors="pt", truncation=True).to(self.model.device)
        prompt_length = len(inputs.input_ids[0])
        
        # Calculate safe max_tokens if not provided
        if max_tokens is None:
            max_tokens = self._get_safe_max_tokens(prompt_length)
            
        logger.info(f"Generating with {max_tokens} tokens (prompt: {prompt_length} tokens)")
        
        with torch.no_grad():
            outputs = self.model.generate(
                inputs.input_ids,
                max_new_tokens=max_tokens,
                do_sample=True,
                temperature=0.7,
                pad_token_id=self.tokenizer.pad_token_id,
                eos_token_id=self.tokenizer.eos_token_id
            )
        
        response = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
        return response[len(prompt):].strip()

    def _generate_with_api(self, prompt: str, max_tokens: Optional[int] = None) -> str:
        """Generate text using API models"""
        # For APIs, we need to estimate prompt tokens
        prompt_tokens = len(prompt.split()) * 1.3  # Rough estimation
        
        # Calculate safe max_tokens if not provided
        if max_tokens is None:
            max_tokens = self._get_safe_max_tokens(int(prompt_tokens))
            
        logger.info(f"Generating with {max_tokens} tokens (estimated prompt: {int(prompt_tokens)} tokens)")
        
        response = self.openai_client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": "You are an expert educator creating a coherent lecture transcript."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=max_tokens
        )
        return response.choices[0].message.content.strip()

    def _generate_text(self, prompt: str, max_tokens: int = 1000) -> str:
        """Generate text using selected model"""
        if self.model_type in self.LOCAL_MODEL_IDS:
            return self._generate_with_local_model(prompt, max_tokens)
        else:
            return self._generate_with_api(prompt, max_tokens)

    def _validate_word_count(self, total_words: int, target_words: int, min_words: int, max_words: int) -> None:
        """Validate word count with flexible thresholds and log warnings/errors"""
        deviation = abs(total_words - target_words) / target_words
        
        if deviation > self.LARGE_DEVIATION_THRESHOLD:
            logger.error(
                f"Word count {total_words} significantly outside target range "
                f"({min_words}-{max_words}). Deviation: {deviation:.2%}"
            )
        elif total_words < min_words or total_words > max_words:
            logger.warning(
                f"Word count {total_words} slightly outside target range "
                f"({min_words}-{max_words}). Deviation: {deviation:.2%}"
            )
            
    def transform_to_lecture(self, 
                           text: str, 
                           target_duration: int = 30,
                           include_examples: bool = True) -> str:
        """
        Transform input text into a structured teaching transcript
        
        Args:
            text: Input transcript text
            target_duration: Target lecture duration in minutes
            include_examples: Whether to include practical examples
            
        Returns:
            str: Generated teaching transcript, regardless of word count validation
        """
        logger.info(f"Starting transformation for {target_duration} minute lecture")
        
        # Clean and preprocess text
        cleaned_text = self.text_processor.clean_text(text)
        input_words = self.text_processor.count_words(cleaned_text)
        logger.info(f"Input text cleaned. Word count: {input_words}")
        
        # Calculate target word count
        target_words = self.words_per_minute * target_duration
        min_words = int(target_words * 0.95)  # Minimum 95% of target
        max_words = int(target_words * 1.05)  # Maximum 105% of target
        
        logger.info(f"Target word count: {target_words} (min: {min_words}, max: {max_words})")
        
        # Generate detailed lecture structure with topics
        structure_data = self._generate_detailed_structure(cleaned_text, target_duration)
        logger.info("Detailed lecture structure generated")
        logger.info(f"Topics identified: {[t['title'] for t in structure_data['topics']]}")
        
        # Calculate section word counts
        section_words = {
            'intro': int(target_words * 0.1),
            'main': int(target_words * 0.7),
            'practical': int(target_words * 0.15),
            'summary': int(target_words * 0.05)
        }
        
        try:
            logger.info("Generating content by sections with topic tracking")
            
            # Introduction with learning objectives and topic preview
            intro = self._generate_section(
                'introduction',
                structure_data,
                cleaned_text,
                section_words['intro'],
                include_examples,
                is_first=True
            )
            intro_words = self.text_processor.count_words(intro)
            logger.info(f"Introduction generated: {intro_words} words")
            
            # Track context for coherence
            context = {
                'current_section': 'introduction',
                'covered_topics': [],
                'pending_topics': [t['title'] for t in structure_data['topics']],
                'key_terms': set(),
                'current_narrative': intro[-1000:],  # Last 1000 words for context
                'learning_objectives': structure_data['learning_objectives']
            }
            
            # Main content with topic progression
            main_content = self._generate_main_content(
                structure_data,
                cleaned_text,
                section_words['main'],
                include_examples,
                context
            )
            main_words = self.text_processor.count_words(main_content)
            logger.info(f"Main content generated: {main_words} words")
            
            # Update context after main content
            context['current_section'] = 'main'
            context['current_narrative'] = main_content[-1000:]
            
            # Practical applications tied to main topics
            practical = self._generate_section(
                'practical',
                structure_data,
                cleaned_text,
                section_words['practical'],
                include_examples,
                context=context
            )
            practical_words = self.text_processor.count_words(practical)
            logger.info(f"Practical section generated: {practical_words} words")
            
            # Update context for summary
            context['current_section'] = 'practical'
            context['current_narrative'] = practical[-500:]
            
            # Summary with topic reinforcement
            summary = self._generate_section(
                'summary',
                structure_data,
                cleaned_text,
                section_words['summary'],
                include_examples,
                is_last=True,
                context=context
            )
            summary_words = self.text_processor.count_words(summary)
            logger.info(f"Summary generated: {summary_words} words")
            
            # Combine all sections
            full_content = f"{intro}\n\n{main_content}\n\n{practical}\n\n{summary}"
            total_words = self.text_processor.count_words(full_content)
            logger.info(f"Total content generated: {total_words} words")
            
            # Log warnings/errors but don't raise exceptions
            self._validate_word_count(total_words, target_words, min_words, max_words)
            
            # Validate coherence
            self._validate_coherence(full_content, structure_data)
            logger.info("Content coherence validated")
            
            return full_content
            
        except Exception as e:
            logger.error(f"Error during content generation: {str(e)}")
            # If we have partial content, return it
            if 'full_content' in locals():
                logger.warning("Returning partial content despite errors")
                return full_content
            raise  # Re-raise only if we have no content at all
            
    def _generate_detailed_structure(self, text: str, target_duration: int) -> Dict:
        """Generate detailed lecture structure with topics and objectives"""
        logger.info("Generating detailed lecture structure")
        
        prompt = f"""
        You are an expert educator creating a detailed lecture outline.
        Analyze this transcript and create a structured JSON output with the following:
        
        1. Title of the lecture
        2. 3-5 clear learning objectives
        3. 3-4 main topics, each with:
           - Title
           - Key concepts
           - Subtopics
           - Time allocation (in minutes)
           - Connection to learning objectives
        4. Practical application ideas
        5. Key terms to track
        
        IMPORTANT: Response MUST be valid JSON. Format exactly like this, with no additional text:
        {{
            "title": "string",
            "learning_objectives": ["string"],
            "topics": [
                {{
                    "title": "string",
                    "key_concepts": ["string"],
                    "subtopics": ["string"],
                    "duration_minutes": number,
                    "objective_links": [number]
                }}
            ],
            "practical_applications": ["string"],
            "key_terms": ["string"]
        }}
        
        Target duration: {target_duration} minutes
        
        Transcript excerpt:
        {text[:2000]}
        """
        
        try:
            # First attempt with direct JSON generation
            response = self.openai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "You are an expert educator. Output ONLY valid JSON, no other text."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=2000
            )
            
            content = response.choices[0].message.content.strip()
            logger.debug(f"Raw structure response: {content}")
            
            try:
                structure_data = json.loads(content)
                logger.info("Structure data parsed successfully")
                return structure_data
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON directly: {str(e)}")
                
                # Try to extract JSON if it's wrapped in other text
                import re
                json_match = re.search(r'({[\s\S]*})', content)
                if json_match:
                    try:
                        structure_data = json.loads(json_match.group(1))
                        logger.info("Structure data extracted and parsed successfully")
                        return structure_data
                    except json.JSONDecodeError:
                        logger.warning("Failed to parse extracted JSON")
                
                # If both attempts fail, use fallback structure
                logger.warning("Using fallback structure")
                return self._generate_fallback_structure(text, target_duration)
                
        except Exception as e:
            logger.error(f"Error generating structure: {str(e)}")
            return self._generate_fallback_structure(text, target_duration)
            
    def _generate_fallback_structure(self, text: str, target_duration: int) -> Dict:
        """Generate a basic fallback structure when JSON parsing fails"""
        logger.info("Generating fallback structure")
        
        # Generate a simpler structure prompt
        prompt = f"""
        Analyze this text and provide:
        1. A title (one line)
        2. Three learning objectives (one per line)
        3. Three main topics (one per line)
        4. Three key terms (one per line)
        
        Text: {text[:1000]}
        """
        
        try:
            response = self.openai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "You are an expert educator. Provide concise, line-by-line responses."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=1000
            )
            
            lines = response.choices[0].message.content.strip().split('\n')
            lines = [line.strip() for line in lines if line.strip()]
            
            # Extract components from lines
            title = lines[0] if lines else "Lecture"
            objectives = [obj for obj in lines[1:4] if obj][:3]
            topics = [topic for topic in lines[4:7] if topic][:3]
            terms = [term for term in lines[7:10] if term][:3]
            
            # Calculate minutes per topic
            main_time = int(target_duration * 0.7)  # 70% for main content
            topic_minutes = main_time // len(topics)
            
            # Create fallback structure
            return {
                "title": title,
                "learning_objectives": objectives,
                "topics": [
                    {
                        "title": topic,
                        "key_concepts": [topic],  # Use topic as key concept
                        "subtopics": ["Overview", "Details", "Examples"],
                        "duration_minutes": topic_minutes,
                        "objective_links": [1]  # Link to first objective
                    }
                    for topic in topics
                ],
                "practical_applications": [
                    "Real-world application example",
                    "Interactive exercise",
                    "Case study"
                ],
                "key_terms": terms
            }
            
        except Exception as e:
            logger.error(f"Error generating fallback structure: {str(e)}")
            # Return minimal valid structure
            return {
                "title": "Lecture Overview",
                "learning_objectives": ["Understand key concepts", "Apply knowledge", "Analyze examples"],
                "topics": [
                    {
                        "title": "Main Topic",
                        "key_concepts": ["Core concept"],
                        "subtopics": ["Overview"],
                        "duration_minutes": target_duration // 2,
                        "objective_links": [1]
                    }
                ],
                "practical_applications": ["Practical example"],
                "key_terms": ["Key term"]
            }
        
    def _generate_section(self,
                         section_type: str,
                         structure_data: Dict,
                         original_text: str,
                         target_words: int,
                         include_examples: bool,
                         context: Dict = None,
                         is_first: bool = False,
                         is_last: bool = False) -> str:
        """Generate content for a specific section with coherence tracking"""
        logger.info(f"Generating {section_type} section (target: {target_words} words)")
        
        # Base prompt with structure
        prompt = f"""
        You are an expert educator creating a detailed lecture transcript.
        Generate the {section_type} section with EXACTLY {target_words} words.
        
        Lecture Title: {structure_data['title']}
        Learning Objectives: {', '.join(structure_data['learning_objectives'])}
        
        Current section purpose:
        """
        
        # Add section-specific guidance
        if section_type == 'introduction':
            prompt += """
            - Start with an engaging hook
            - Present clear learning objectives
            - Preview main topics
            - Set expectations for the lecture
            """
        elif section_type == 'main':
            prompt += f"""
            - Cover these topics: {[t['title'] for t in structure_data['topics']]}
            - Build progressively on concepts
            - Include clear transitions
            - Reference previous concepts
            """
        elif section_type == 'practical':
            prompt += """
            - Apply concepts to real-world scenarios
            - Connect to previous topics
            - Include interactive elements
            - Reinforce key learning points
            """
        elif section_type == 'summary':
            prompt += """
            - Reinforce key takeaways
            - Connect back to objectives
            - Provide next steps
            - End with a strong conclusion
            """
            
        # Add context if available
        if context:
            prompt += f"""
            
            Context:
            - Covered topics: {', '.join(context['covered_topics'])}
            - Pending topics: {', '.join(context['pending_topics'])}
            - Key terms used: {', '.join(context['key_terms'])}
            - Recent narrative: {context['current_narrative']}
            """
            
        # Add requirements
        prompt += f"""
        
        Requirements:
        1. STRICT word count: Generate EXACTLY {target_words} words
        2. Include practical examples: {include_examples}
        3. Use clear transitions
        4. Include engagement points
        5. Use time markers [MM:SS]
        6. Reference specific content from transcript
        7. Maintain narrative flow
        8. Use key terms consistently
        """
        
        response = self.openai_client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": "You are an expert educator creating a coherent lecture transcript."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=8000
        )
        
        content = response.choices[0].message.content
        word_count = self.text_processor.count_words(content)
        logger.info(f"Section generated: {word_count} words")
        
        return content
        
    def _generate_main_content(self,
                             structure_data: Dict,
                             original_text: str,
                             target_words: int,
                             include_examples: bool,
                             context: Dict) -> str:
        """Generate main content with topic progression"""
        logger.info(f"Generating main content (target: {target_words} words)")
        
        # Calculate words per topic based on their duration ratios
        total_duration = sum(t['duration_minutes'] for t in structure_data['topics'])
        topic_words = {}
        
        for topic in structure_data['topics']:
            ratio = topic['duration_minutes'] / total_duration
            topic_words[topic['title']] = int(target_words * ratio)
            
        logger.info(f"Topic word allocations: {topic_words}")
        
        # Generate content for each topic
        topic_contents = []
        
        for topic in structure_data['topics']:
            topic_target = topic_words[topic['title']]
            
            # Update context for topic
            context['current_topic'] = topic['title']
            context['covered_topics'].append(topic['title'])
            context['pending_topics'].remove(topic['title'])
            context['key_terms'].update(topic['key_concepts'])
            
            # Generate topic content
            topic_content = self._generate_section(
                f"main_topic_{topic['title']}",
                structure_data,
                original_text,
                topic_target,
                include_examples,
                context=context
            )
            
            topic_contents.append(topic_content)
            context['current_narrative'] = topic_content[-1000:]
            
        return "\n\n".join(topic_contents)
        
    def _validate_coherence(self, content: str, structure_data: Dict):
        """Validate content coherence against structure"""
        logger.info("Validating content coherence")
        
        # Check for learning objectives
        for objective in structure_data['learning_objectives']:
            if not any(term.lower() in content.lower() for term in objective.split()):
                logger.warning(f"Learning objective not well covered: {objective}")
                
        # Check for key terms
        for term in structure_data['key_terms']:
            if content.lower().count(term.lower()) < 2:
                logger.warning(f"Key term underutilized: {term}")
                
        # Check topic coverage
        for topic in structure_data['topics']:
            if not any(concept.lower() in content.lower() for concept in topic['key_concepts']):
                logger.warning(f"Topic concepts not well covered: {topic['title']}")
                
        logger.info("Coherence validation complete") 