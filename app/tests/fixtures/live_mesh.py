"""MeSH terms captured from a real PubMed run, 2026-08-30.

Ten randomised trials of time-restricted eating, fetched live during the first run of
the evidence pipeline. They are here because that run exposed a design fault: clustering
keyed on the model-extracted intervention phrase, which came back differently worded for
every paper, so ten trials of one intervention produced ten clusters and nothing was ever
synthesised across sources.

Every one of these records carries the MeSH descriptor "Intermittent Fasting", assigned by
a human indexer. The fix keys on that instead, and this fixture is the regression test.
"""

from __future__ import annotations

LIVE_TRE_RECORDS: list[dict] = [
    {
        "source_key": "doi:10.1016/j.nutres.2026.07.006",
        "title": "Protocol for the CLOCKS study: A 4-arm randomized controlled trial testing the independent and combined effects of 2 chronotherapies-early time-restricted eating and daytime bright light therapy-in adults with type 2 diabetes.",
        "mesh_terms": [
            "Humans",
            "Diabetes Mellitus, Type 2",
            "Intermittent Fasting",
            "Circadian Rhythm",
            "Phototherapy",
            "Blood Glucose",
            "Chronotherapy",
            "Female",
            "Male",
            "Middle Aged",
            "Glycated Hemoglobin",
            "Adult",
            "Glycemic Control",
            "Blue Light",
            "Melatonin"
        ],
        "intervention": "early time-restricted eating (eTRE) and/or daytime bright light therapy (BLT)"
    },
    {
        "source_key": "doi:10.3390/nu18152470",
        "title": "Effects of Nordic Walking Combined with High-Protein Intake and Time-Restricted Eating on Muscle-Related Outcomes in Postmenopausal Women: A Three-Arm Randomized Controlled Trial.",
        "mesh_terms": [
            "Humans",
            "Female",
            "Postmenopause",
            "Walking",
            "Middle Aged",
            "Hand Strength",
            "Intermittent Fasting",
            "Dietary Proteins",
            "Muscle, Skeletal",
            "Aged"
        ],
        "intervention": None
    },
    {
        "source_key": "doi:10.1016/j.actpsy.2026.107510",
        "title": "Impact of a 4-week time-restricted eating (16:8) intervention on working memory and attentional control in adult women: A randomized controlled pilot.",
        "mesh_terms": [
            "Humans",
            "Female",
            "Memory, Short-Term",
            "Pilot Projects",
            "Intermittent Fasting",
            "Attention",
            "Adult",
            "Young Adult",
            "Body Mass Index",
            "Cognition",
            "Body Weight",
            "Fasting",
            "Executive Function"
        ],
        "intervention": "16:8 TRE regimen (16 h fasting, 8 h eating)"
    },
    {
        "source_key": "doi:10.1002/oby.70270",
        "title": "Time-Restricted Eating on Appetite and Inflammation in Adults With Obesity and Prediabetes: A Randomized Feeding Study.",
        "mesh_terms": [
            "Humans",
            "Middle Aged",
            "Female",
            "Male",
            "Obesity",
            "Intermittent Fasting",
            "Prediabetic State",
            "Inflammation",
            "Appetite",
            "Feeding Behavior",
            "Weight Loss",
            "Aged",
            "Leptin",
            "Diabetes Mellitus, Type 2",
            "Ghrelin",
            "Adiponectin",
            "Hunger",
            "C-Reactive Protein",
            "Surveys and Questionnaires",
            "Body Mass Index",
            "Fasting",
            "Hydrocortisone",
            "Adult"
        ],
        "intervention": "10-h time-restricted eating (TRE)"
    },
    {
        "source_key": "doi:10.3390/nu18132053",
        "title": "Changes in Resting Energy Expenditure in Response to Different Dietary Patterns: A Randomized Clinical Trial Exploratory Sub-Analysis.",
        "mesh_terms": [
            "Humans",
            "Female",
            "Middle Aged",
            "Adult",
            "Male",
            "Obesity",
            "Caloric Restriction",
            "Intermittent Fasting",
            "Diet, Mediterranean",
            "Energy Metabolism",
            "Diet, Ketogenic",
            "Fasting",
            "Basal Metabolism"
        ],
        "intervention": None
    },
    {
        "source_key": "doi:10.1007/s00394-026-04034-3",
        "title": "Effect of two types of time-restricted eating on glycemic, lipid indices, and weight in women with polycystic ovary syndrome: a randomized controlled trial.",
        "mesh_terms": [
            "Humans",
            "Female",
            "Polycystic Ovary Syndrome",
            "Intermittent Fasting",
            "Adult",
            "Blood Glucose",
            "Young Adult",
            "Lipids",
            "Insulin",
            "Waist Circumference",
            "Body Mass Index",
            "Insulin Resistance",
            "Body Weight",
            "Glycemic Index",
            "Fasting"
        ],
        "intervention": "early time-restricted eating (eTRE) and mid-day time-restricted eating (mTRE)"
    },
    {
        "source_key": "doi:10.1016/j.clnu.2026.106706",
        "title": "Effects of an early, late, and self-selected time-restricted eating intervention on weight loss maintenance in adults with overweight or obesity: A 12-month follow-up of a randomized controlled trial.",
        "mesh_terms": [
            "Humans",
            "Female",
            "Middle Aged",
            "Obesity",
            "Intermittent Fasting",
            "Follow-Up Studies",
            "Overweight",
            "Weight Loss",
            "Male",
            "Adult",
            "Body Mass Index",
            "Time Factors",
            "Feeding Behavior"
        ],
        "intervention": "8-h eating window starting before 10:00, 8-h eating window starting after 13:00, participant chosen 8-h eating window"
    },
    {
        "source_key": "doi:10.3390/nu18111824",
        "title": "Relationship Between Sleep and Meal Timing with Glycemia Parameters in Individuals with Obesity Participating in a Randomized Time-Restricted Eating Study.",
        "mesh_terms": [
            "Humans",
            "Adult",
            "Middle Aged",
            "Obesity",
            "Female",
            "Male",
            "Blood Glucose",
            "Intermittent Fasting",
            "Meals",
            "Sleep",
            "Continuous Glucose Monitoring",
            "Young Adult",
            "Adolescent",
            "Aged",
            "Time Factors",
            "Circadian Rhythm",
            "Feeding Behavior",
            "Caloric Restriction",
            "Sleep Duration",
            "Actigraphy"
        ],
        "intervention": "TRE (8 h eating window), CR (15% reduction in daily caloric intake), and UE (usual eating habits)"
    },
    {
        "source_key": "doi:10.1111/jhn.70293",
        "title": "Effect of Time-Restricted Eating on Metabolic Adaptation in Adults With Severe Obesity During Early Phase of Weight Loss.",
        "mesh_terms": [
            "Humans",
            "Weight Loss",
            "Adult",
            "Basal Metabolism",
            "Intermittent Fasting",
            "Body Composition",
            "Female",
            "Middle Aged",
            "Adaptation, Physiological",
            "Male",
            "Thermogenesis",
            "Obesity, Morbid",
            "Caloric Restriction",
            "Energy Metabolism",
            "Calorimetry, Indirect",
            "Young Adult",
            "Leptin",
            "Diet, Reducing",
            "Appetite",
            "Absorptiometry, Photon",
            "Energy Intake"
        ],
        "intervention": None
    },
    {
        "source_key": "doi:10.1007/s00125-026-06762-x",
        "title": "Time-restricted eating versus dietetic guidance on glycaemic outcomes in adults at risk of type 2 diabetes: a non-inferiority randomised clinical trial.",
        "mesh_terms": [
            "Humans",
            "Diabetes Mellitus, Type 2",
            "Female",
            "Male",
            "Middle Aged",
            "Glycated Hemoglobin",
            "Blood Glucose",
            "Intermittent Fasting",
            "Adult",
            "Aged",
            "Energy Intake",
            "Fasting",
            "Dietetics"
        ],
        "intervention": "Time-restricted eating (TRE) consolidates daily energy intake to a consistent 6-10 h window"
    }
]
