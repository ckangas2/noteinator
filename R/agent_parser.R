library(tidyverse)
library(stringr)

# --- CONFIGURATION ---
BASE_DIR <- "C:/Users/chase/Documents/Everything_Python/Scribe"
INPUT_FILE <- file.path(BASE_DIR, "lab_notebook.csv")
OUTPUT_FILE <- file.path(BASE_DIR, "data/processed_experiment.csv")

parse_lab_notebook <- function() {
  
  if (!file.exists(INPUT_FILE)) return(NULL)
  
  # 1. Load Raw Data
  raw_data <- read_csv(INPUT_FILE, col_types = cols(), show_col_types = FALSE) %>%
    filter(!is.na(Raw_Entry))
  
  # 2. THE TOKENIZER (New Logic)
  # We split the spoken paragraph into multiple rows wherever "Sample" appears.
  # This lets you say "Sample 1 is X. Sample 2 is Y" and get two rows.
  exploded_data <- raw_data %>%
    # Add a delimiter before every "Sample" or "sample"
    mutate(Clean_Entry = str_replace_all(Raw_Entry, "(?i)sample", "|SAMPLE")) %>%
    # Split the row into multiple rows based on the delimiter
    separate_rows(Clean_Entry, sep = "\\|") %>%
    filter(str_detect(Clean_Entry, "SAMPLE")) # Keep only the chunks with data
  
  processed <- exploded_data %>%
    mutate(
      # --- A. Extract Sample ID ---
      # Looks for "SAMPLE 4", "SAMPLE A2"
      Sample_ID = str_extract(Clean_Entry, "SAMPLE\\s?[0-9A-Z]+"),
      Sample_ID = str_remove(Sample_ID, "SAMPLE\\s?"),
      
      # --- B. Extract Experiment ID ---
      # Inherits from the main raw entry (if you said it at the start)
      Exp_String = str_extract(Raw_Entry, "(?i)experiment\\s?([A-Za-z0-9]+)"),
      Experiment_ID = str_to_title(str_remove(Exp_String, "(?i)experiment\\s?")),
      Experiment_ID = replace_na(Experiment_ID, "General"),
      
      # --- C. Extract Numbers (Robust) ---
      
      # 1. Coefficient: "1.5 times", "2 x", "5 *"
      Coeff_String = str_extract(Clean_Entry, "(?i)(\\d+\\.?\\d*)\\s?(x|times?|\\*)\\s?10"),
      Coefficient = as.numeric(str_extract(Coeff_String, "\\d+\\.?\\d*")),
      
      # 2. Exponent: "10 to the 8", "10^8", "E8"
      # We look for digits AFTER "10 to the"
      Exp_String_Full = str_extract(Clean_Entry, "(?i)(10\\s?to\\s?the\\s?|\\^|E)\\s?\\d+"),
      Exponent = as.numeric(str_extract(Exp_String_Full, "\\d+")), # Just grab the digits from the match
      
      # 3. Log Fallback: "5 log"
      Log_Val = as.numeric(str_extract(Clean_Entry, "(?i)(\\d+\\.?\\d*)\\s?log")),
      
      # --- D. Calculate Final Titer ---
      Titer = case_when(
        !is.na(Exponent) & !is.na(Coefficient) ~ Coefficient * (10^Exponent),
        !is.na(Exponent) ~ 1 * (10^Exponent), # Implied 1x
        !is.na(Log_Val) ~ 10^Log_Val,
        TRUE ~ NA_real_
      ),
      
      # --- E. Status Flags ---
      Status = case_when(
        str_detect(Clean_Entry, "(?i)weird|strange|fail") ~ "Flagged",
        str_detect(Clean_Entry, "(?i)toxic|death") ~ "Toxic",
        !is.na(Titer) ~ "Valid",
        TRUE ~ "No Data"
      )
    ) %>%
    filter(!is.na(Sample_ID)) %>%
    # Dedup: If you corrected yourself, take the latest entry for that Sample
    group_by(Experiment_ID, Sample_ID) %>%
    slice_tail(n = 1) %>% 
    ungroup()
  
  # Ensure folder exists
  if(!dir.exists(dirname(OUTPUT_FILE))) dir.create(dirname(OUTPUT_FILE), recursive = TRUE)
  
  write_csv(processed, OUTPUT_FILE)
  return(processed)
}