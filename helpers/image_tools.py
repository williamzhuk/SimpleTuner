from PIL import Image

def calculate_luminance(image_path):
    img = Image.open(image_path)
    pixels = list(img.getdata())
    
    luminance_values = []
    for pixel in pixels:
        r, g, b = pixel[:3]  # Assuming the image is RGB or RGBA
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        luminance_values.append(luminance)
    
    # Return average luminance for the entire image
    avg_luminance = sum(luminance_values) / len(luminance_values)
    return avg_luminance