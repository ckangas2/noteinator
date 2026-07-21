library(tidyverse)
library(scales)
library(janitor)

# --- CONFIGURATION ---
BASE_DIR <- "C:/Users/chase/Documents/Everything_Python/Scribe"
INPUT_FILE <- file.path(BASE_DIR, "data/processed_experiment.csv")
DASHBOARD_IMAGE <- file.path(BASE_DIR, "dashboard_view.png")

last_mtime <- 0
print("Starting The Dashboard Watcher... (Robust Mode)")

while(TRUE) {
  if (file.exists(INPUT_FILE)) {
    current_mtime <- file.info(INPUT_FILE)$mtime
    
    if (current_mtime > last_mtime) {
      print(paste("New Data Detected:", Sys.time()))
      
      tryCatch({
        # 1. READ
        raw_data <- read_csv(INPUT_FILE, show_col_types = FALSE)
        
        # 2. CLEAN NAMES
        data <- raw_data %>%
          janitor::clean_names() # "Sample_Id" -> "sample_id"
        
        # 3. DEFENSIVE CHECKS (The Fix)
        # If specific columns are missing, add them with defaults
        if(!"experiment_id" %in% names(data)) {
          data$experiment_id <- "General"
        }
        if(!"sample_id" %in% names(data)) {
          # Fallback: try to find anything that looks like an ID
          if("sample" %in% names(data)) data$sample_id <- data$sample
          else data$sample_id <- "Unknown"
        }
        
        # 4. PLOT
        if (nrow(data) > 0) {
          p <- ggplot(data, aes(x = as.character(sample_id), y = titer, fill = status)) +
            geom_col(alpha = 0.9, width = 0.7) +
            scale_y_log10(
              breaks = trans_breaks("log10", function(x) 10^x),
              labels = trans_format("log10", math_format(10^.x))
            ) +
            facet_wrap(~experiment_id, scales = "free_x") +
            
            scale_fill_manual(values = c("Valid" = "#27ae60", "Toxic" = "#c0392b", "Flagged" = "#f39c12", "Invalid" = "#c0392b")) +
            theme_minimal(base_size = 18) +
            labs(
              title = "Live Experiment Dashboard",
              subtitle = paste("Last Update:", format(Sys.time(), "%H:%M:%S")),
              y = "Viral Titer (PFU/mL)",
              x = "Sample ID"
            )
          
          ggsave(DASHBOARD_IMAGE, plot = p, width = 12, height = 7, dpi = 100)
          print(">> Dashboard Repainted.")
        }
      }, error = function(e) {
        print(paste("Data Read Error (Retrying...):", e$message))
      })
      
      last_mtime <- current_mtime
    }
  }
  Sys.sleep(2)
}